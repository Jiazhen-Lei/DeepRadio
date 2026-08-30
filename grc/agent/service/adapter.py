"""adapter: GUI 守门 —— ServiceAgent.step -> AgentReply / .grc。

主路径: WorkflowEngine 驱动 Stage；主机控制面执行 BLE/硬件门；
可选 deepagents。未装 LLM 时走确定性 handler / design_link。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from ..schema import AgentReply, ToolInvocation
from ..llm import SemanticUnderstandingError
from ..memory.profile import UserProfile
from ..state import (
    Claim,
    ClaimStore,
    Decision,
    Evidence,
    SharedState,
    WorkflowDecision,
)
from ..tools.registry import ToolContext
from ..tools.hardware_profiles import device_args_for, normalize_hardware
from ..workflow import WorkflowEngine
from ..workflow.completion import (
    EXTERNAL_PRECONDITION_COMPLETIONS,
    external_waiting_note,
)
from ..workflow.intent_alignment import IntentAlignmentCoordinator
from ..workflow.planning import is_rf_grant_effect, stage_display_label
from ..workflow.revision import analyze_intent_patch
from . import orchestrator as _orch
from . import session_store as _store
from . import result_projector as _projector
from . import stage_executor as _stage_executor

logger = logging.getLogger(__name__)

DEFAULT_RECURSION_LIMIT = 150
MAX_AUTONOMOUS_STAGES_PER_TURN = 16

# Safety-critical and deterministic protocol stages are executed by the host
# control plane.  The LLM still creates/routs the Workflow, but cannot omit,
# reorder, or duplicate hardware gates by choosing tools opportunistically.
_HOST_CONTROLLED_STAGES = frozenset({
    "hardware_precheck",
    "configure_and_check",
    "build_ble_advertiser",
    "offline_protocol_verify",
    "discover_and_probe_device",
    "discover_and_probe_hardware",
    "configure_device",
    "transmit_bounded",
    "run_bounded",
    "stop_and_finalize",
    "stop_runtime",
})

# User-facing observation channel: omit routing/tool bookkeeping so a fast
# deterministic stage does not flood GTK with log-like redraws.
_PROGRESS_EVENTS = frozenset({
    "intent_draft_created",
    "interaction_requested",
    "intent_confirmed",
    "intent_patch_proposed",
    "intent_patch_applied",
    "workflow_created",
    "workflow_recovered",
    "workflow_completed",
    "workflow_cancelled",
    "stage_started",
    "stage_completed",
    "stage_errored",
    "stage_invalidated",
    "stage_skipped",
    "checkpoint_opened",
    "checkpoint_resolved",
})


def _flowgraph_semantic_hash(path: str) -> str:
    """Hash functional GRC content while ignoring canvas-only formatting."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        from grc.core.io import yaml

        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, TypeError, ValueError):
        return ""

    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: normalize(item)
                for key, item in value.items()
                if key not in {"coordinate", "rotation"}
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    encoded = json.dumps(
        normalize(data), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recursion_limit() -> int:
    raw = (os.environ.get("GRC_AGENT_RECURSION_LIMIT") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RECURSION_LIMIT
    return value if value > 0 else DEFAULT_RECURSION_LIMIT


_DETERMINISTIC_PROGRESS = {
    "build_ble_advertising_pdu": "✓ BLE PDU generated",
    "generate_ble_1m_waveform": "✓ IQ waveform generated",
    "verify_ble_packet_bits": "✓ Offline verification passed",
    "validate_flowgraph": "✓ Flowgraph structure validated",
    "build_ble_pluto_tx_flowgraph": "✓ PlutoSDR TX flowgraph generated",
    "build_ble_uhd_tx_flowgraph": "✓ B210 TX flowgraph generated",
    "discover_devices": "✓ SDR discovered",
    "probe_device": "✓ SDR probed",
    "configure_sdr": "✓ SDR configuration recorded",
    "arm_hardware_flowgraph": "✓ Flowgraph armed",
    "start_flowgraph": "✓ Bounded TX started",
    "stop_flowgraph": "✓ Bounded TX stopped",
}

#: User-facing explanations for completion predicates.  Internal predicate
#: names and failure codes must never reach the user reply directly; they stay
#: in events.jsonl for audit.
_COMPLETION_EXPLANATIONS = {
    "flowgraph_saved": "the flowgraph file was not saved",
    "structural_validation_completed": "the flowgraph did not pass structural validation",
    "hardware_endpoint_present": "the flowgraph has no active SDR hardware output yet",
    "radio_parameters_match": (
        "the hardware parameters do not match the requested "
        "frequency or sample rate"
    ),
    "realtime_sink_present": "no live spectrum display is present",
    "device_discovered": "no SDR device was discovered",
    "device_probed": "the SDR device could not be probed",
    "device_identity_matched": "the connected device does not match the requested SDR model",
    "flowgraph_armed": "the hardware output has not been armed yet",
    "ble_packet_created": "the BLE packet was not created",
    "ble_waveform_generated": "the BLE waveform was not generated",
    "ble_packet_valid": "the BLE packet did not pass offline verification",
    "transmit_started": "the transmission did not start",
    "transmit_stopped": "the transmission did not stop cleanly",
    "runtime_started": "the runtime did not start",
    "over_air_observed": "the target signal has not been observed over the air yet",
    "measurement_completed": "the measurement was not completed",
    "receive_quality_evaluated": "the receive quality was not evaluated",
    "diagnosis_created": "no diagnosis was produced",
    "repair_applied": "the repair was not applied",
    "change_plan_created": "no change plan was produced",
    "configuration_recorded": "the device configuration has not been recorded",
    "hardware_check_completed": "the hardware check did not complete",
    "hardware_precheck_completed": "the hardware pre-check did not complete",
}

_FAILURE_CODE_EXPLANATIONS = {
    "REPLY_STATUS_REJECTED": "the stage could not produce a valid result",
    "PROTOCOL_INVALID_JSON": "the stage reported a malformed result",
}


def _friendly_stage_failure(
    missing_completion: List[str],
    failure_codes: List[str],
) -> str:
    """Render a failed stage as phenomenon + cause + next step.

    Internal predicate names and codes (``MISSING_COMPLETION:*``,
    ``REPLY_STATUS_REJECTED`` …) are translated to plain language; the raw
    codes remain in the session event stream for audit.
    """
    reasons: List[str] = []
    for name in missing_completion:
        reasons.append(
            _COMPLETION_EXPLANATIONS.get(name, str(name).replace("_", " "))
        )
    for code in failure_codes:
        if code.startswith("MISSING_COMPLETION:"):
            inner = code.split(":", 1)[1]
            reasons.append(
                _COMPLETION_EXPLANATIONS.get(
                    inner, inner.replace("_", " ")
                )
            )
            continue
        reasons.append(
            _FAILURE_CODE_EXPLANATIONS.get(
                code, str(code).replace("_", " ").lower()
            )
        )
    reasons = list(dict.fromkeys(item for item in reasons if item))
    body = "; ".join(reasons) if reasons else "the stage could not complete"
    return (
        "This stage did not finish: {}. The current project is unchanged. "
        "You can retry, adjust the parameters, or ask me to diagnose the "
        "problem.".format(body)
    )


def _read_log_tail(path: str, limit: int = 20) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            log_lines = handle.readlines()
    except OSError:
        return ""
    return "".join(log_lines[-limit:]).strip()


def _merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in (src or {}).items():
        if v:
            dst[k] = v

class _ToolCtxShim:
    """兼容 GUI 写法 ``agent.ctx.tool_ctx.out_dir = ...`` 的最小载体。"""

    def __init__(self, out_dir: str = "") -> None:
        self.out_dir = out_dir


class _CtxShim:
    """GUI 兼容层:让 ServiceAgent 暴露 ``.ctx`` 契约(out_dir/adaptive/profile)。

    GUI 通过 ``agent.ctx.tool_ctx.out_dir`` / ``agent.ctx.adaptive``
    / ``agent.ctx.profile`` 三个运行期旋钮驱动。ServiceAgent 不再自研
    ctx,但为让 AgentPanel 零改动,这里提供同形状的兼容视图,读写直接落到
    ServiceAgent 上。
    """

    def __init__(self, agent: "ServiceAgent") -> None:
        self._agent = agent
        self.tool_ctx = _ToolCtxShim(out_dir="")
        self.adaptive = True

    @property
    def profile(self) -> UserProfile:
        return self._agent.profile



class ServiceAgent:
    """DeepRadio 服务级 Agent,对 GUI 暴露 AgentReply 契约。"""

    def __init__(self, session_id: Optional[str] = None,
                 profile: Any = None, platform: Any = None):
        self.session_id = session_id or f"gui-{uuid.uuid4().hex[:8]}"
        self._progress_listeners = []
        _store.ensure_run_metadata(self.session_id)
        # 统一用 memory.profile.UserProfile(创新 B);GUI 通过 ctx.profile 驱动。
        self.profile = profile if isinstance(profile, UserProfile) \
            else UserProfile()
        self._platform = platform
        self._state = SharedState.load(
            _store.state_path(self.session_id), session_id=self.session_id
        )
        # Waiting/running are runtime facts, not durable Claims.  Migrate the
        # one legacy transient assertion that older sessions persisted.
        self._state.claims = [
            claim for claim in self._state.claims
            if claim.id != "rf_plan_awaiting_decision"
        ]
        self._tool_ctx: Optional[ToolContext] = None
        # Tool events captured by a retry pre-check (device discovery/probe).
        # They are re-injected into the next stage context so completion
        # evaluation sees the CURRENT device state after a hardware retry.
        self._retry_preflight_events: List[Dict[str, Any]] = []
        event_sink = self._sink_engine_event
        try:
            self._workflow = WorkflowEngine(
                _store.workflow_path(self.session_id), event_sink=event_sink
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("workflow.yaml 无法恢复，已归档: %s", exc)
            _store.archive_workflow(self.session_id)
            self._workflow = WorkflowEngine(
                _store.workflow_path(self.session_id), event_sink=event_sink
            )
        self._workflow.reconcile_project_version(
            self._state.project.flowgraph_version
        )
        self._alignment = IntentAlignmentCoordinator(
            self._workflow, self._state, event_sink=self._sink_engine_event
        )
        # GUI 兼容层:agent.ctx.{tool_ctx.out_dir, adaptive, profile}
        self.ctx = _CtxShim(self)
        self._spec_workflow_id = None

    def subscribe_progress(self, callback: Any) -> None:
        """Observe read-only workflow snapshots without gaining control authority."""
        if callable(callback) and callback not in self._progress_listeners:
            self._progress_listeners.append(callback)

    def unsubscribe_progress(self, callback: Any) -> None:
        if callback in self._progress_listeners:
            self._progress_listeners.remove(callback)

    # ---- 上下文装配 --------------------------------------------------
    def _make_ctx(self) -> ToolContext:
        """构造本轮共享 ToolContext(platform + 会话 final 输出目录)。

        Tool 永远写入会话 ``final/``；GUI 指定目录只接收导出副本。这样运行
        白名单、恢复路径和用户可见路径不会指向不同根目录。
        """
        export_dir = _store.nested_export_dir(
            self.session_id, (self.ctx.tool_ctx.out_dir or "").strip()
        )
        out_dir = os.path.join(_store.session_root(self.session_id), "final")
        os.makedirs(out_dir, exist_ok=True)
        platform = self._platform
        if platform is None:
            try:
                from .. import env
                platform = env.make_platform()
                self._platform = platform
            except Exception as exc:  # noqa: BLE001
                logger.info("make_platform 不可用(将由 design_link 如实报告): %s", exc)
        if self._tool_ctx is None:
            ctx = ToolContext(platform=platform, out_dir=out_dir)
            self._tool_ctx = ctx
        else:
            ctx = self._tool_ctx
            ctx.platform = platform
            ctx.out_dir = out_dir
        ctx.extra["profile"] = self.profile
        ctx.extra["state"] = self._state
        ctx.extra["state_path"] = _store.state_path(self.session_id)
        ctx.extra["snapshots_dir"] = _store.snapshots_dir(self.session_id)
        ctx.extra["artifacts"] = {}
        ctx.extra["events"] = []
        if self._retry_preflight_events:
            # Hardware pre-check evidence from a retry must survive the
            # per-stage context reset so completion evaluation can use it.
            ctx.extra["events"].extend(self._retry_preflight_events)
            self._retry_preflight_events = []
        ctx.extra["metrics"] = {}
        ctx.extra["subagent_invocations"] = []
        ctx.extra["export_dir"] = export_dir
        ctx.extra["session_id"] = self.session_id
        ctx.extra["mutation_forbidden"] = False
        ctx.extra["profile_snapshot"] = self.profile.level
        ctx.extra["shared_intent"] = self._state.intent.snapshot()
        ctx.extra.pop("proposed_decisions", None)
        if ctx.flow_graph is None:
            self._load_session_flowgraph(ctx)
        return ctx

    def _refresh_mutation_gate(
        self, ctx: ToolContext, user_text: str, workflow: Any
    ) -> None:
        """Compute write access from semantic intent plus host safety policy.

        An LLM decision may tighten a turn to read-only. It never overrides a
        deterministic forbidden capability or grants a higher effect level.
        """
        forbidden = set()
        task_type = ""
        stage_id = ""
        semantics: Dict[str, Any] = {}
        if workflow is not None:
            forbidden = set(
                (workflow.intent.context or {}).get("forbidden_capabilities") or []
            )
            task_type = str(workflow.task_type or "")
            stage_id = str(workflow.current_stage or "")
            semantics = self._turn_semantics(workflow)
        readonly = "modify_project" in forbidden
        if semantics.get("read_only") is True:
            readonly = True
        elif task_type == "MODIFY_PROJECT" and "modify_project" not in forbidden:
            readonly = False
        if not readonly and stage_id in {
            "apply_and_verify",
            "change_confirmation",
            "repair_and_verify",
        }:
            readonly = False
        ctx.extra["mutation_forbidden"] = bool(readonly)

    @staticmethod
    def _turn_semantics(workflow: Any) -> Dict[str, Any]:
        if workflow is None:
            return {}
        context = dict(getattr(workflow.intent, "context", {}) or {})
        semantics = context.get("turn_semantics")
        return dict(semantics) if isinstance(semantics, dict) else {}

    def _semantic_recipe_switch(self, workflow: Any) -> tuple[str, str]:
        """Return (target_recipe, already_active_label) from LLM semantics."""
        from ..knowledge import recipes

        semantics = self._turn_semantics(workflow)
        target = str(semantics.get("recipe_switch_target") or "").strip()
        if not target:
            target = str((workflow.intent.slots or {}).get("target_recipe") or "").strip()
        if target not in recipes.RECIPES:
            return "", ""
        current = str(self._state.project.config.get("recipe") or "")
        current_mod = str(
            self._state.project.config.get("modulation")
            or recipes.guess_modulation(current)
            or ""
        )
        target_mod = str(recipes.guess_modulation(target) or "")
        if target == current or (
            target_mod and current_mod and target_mod == current_mod
        ):
            return "", (current_mod or target).upper()
        if not (self._state.project.grc_path or current):
            return "", ""
        return target, ""

    def _sync_workflow_intent_to_state(self) -> None:
        """Project canonical Workflow Intent into RadioSpec without reparsing."""
        workflow = self._workflow.workflow
        if workflow is None:
            return
        intent = workflow.intent
        self._alignment.project_confirmed(intent, source="workflow_sync")
        from ..tools.state_tools import (
            ensure_success_condition_claims,
            looks_like_task_dump,
        )

        if self._spec_workflow_id != workflow.workflow_id:
            self._spec_workflow_id = workflow.workflow_id
            self._state.spec.goals = []
            self._state.spec.success_conditions = []
        if (
            intent.raw_text
            and intent.raw_text not in self._state.spec.goals
            and not looks_like_task_dump(intent.raw_text)
        ):
            self._state.spec.goals.append(intent.raw_text)
        ensure_success_condition_claims(
            self._state, list(intent.slots.get("success_conditions") or [])
        )
        stage = self._workflow.current_stage()
        planning = (
            workflow.task_type == "MODIFY_PROJECT"
            and stage is not None
            and stage.id in {
                "inspect_and_plan",
                "change_confirmation",
                "apply_and_verify",
            }
        )
        if (
            str(intent.slots.get("protocol") or "").lower() == "ble"
            and intent.slots.get("operation") == "deploy"
            and intent.slots.get("local_name")
        ):
            condition = (
                "external BLE receiver decodes Complete Local Name="
                f"{intent.slots['local_name']} with captured evidence"
            )
            if condition not in self._state.spec.success_conditions:
                self._state.spec.success_conditions.append(condition)
        spec_keys = (
            ("hardware", "protocol", "local_name")
            if planning
            else ("modulation", "channel", "hardware", "protocol", "local_name")
        )
        for key in spec_keys:
            value = intent.slots.get(key)
            if value in (None, "", []):
                continue
            existing = next(
                (item for item in self._state.spec.decisions if item.key == key),
                None,
            )
            source = intent.slot_sources.get(key, "intent")
            if existing is None:
                self._state.spec.decisions.append(
                    Decision(key=key, value=value, source=source)
                )
            else:
                existing.value = value
                existing.source = source
        for key in (
            "protocol", "hardware", "local_name", "carrier_frequency",
            "sample_rate", "duration_seconds", "max_duration_seconds",
            "bandwidth", "tx_gain", "tx_attenuation", "baseband_kind",
            "tone_frequency_hz", "tone_amplitude", "deploy_permission",
            "terminal_checkpoint", "hardware_access", "ebn0_db",
        ):
            value = intent.slots.get(key)
            if value not in (None, "", []):
                self._state.project.config[key] = value
        channels = intent.slots.get("advertising_channels") or []
        if channels:
            self._state.project.config["advertising_channels"] = list(channels)
            self._state.project.config["ble_channel"] = channels[0]
        self._state.spec.open_questions = list(
            dict.fromkeys(
                list(intent.missing_slots) + list(intent.validation_errors)
            )
        )
        self._sync_control_state()

    def _sync_control_state(self) -> None:
        """Project the compact Workflow control plane into SharedState."""
        _projector.project_control(self._workflow, self._state)

    def _sync_artifact_index(self, manifest_path: str) -> None:
        _projector.project_artifact_index(
            self._state,
            manifest_path,
            workflow=self._workflow.workflow,
        )

    def _load_session_flowgraph(self, ctx: ToolContext) -> None:
        """从会话已保存的 .grc 把内存流图灌进 agent 用的 core Platform。"""
        path = str(self._state.project.grc_path or "")
        path = _store.resolve_session_path(self.session_id, path) or path
        if not path or not os.path.isfile(path) or ctx.platform is None:
            return
        try:
            from grc.core.io import yaml

            with open(path, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle)
            if not isinstance(data, dict):
                return
            flow_graph = ctx.platform.make_flow_graph()
            flow_graph.import_data(data)
            ctx.flow_graph = flow_graph
            ctx.blocks = {}
            for block in getattr(flow_graph, "blocks", []) or []:
                try:
                    bid = str(block.params["id"].get_value())
                except Exception:  # noqa: BLE001
                    bid = str(getattr(block, "name", "") or "")
                if bid and bid != "options":
                    ctx.blocks[bid] = block
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载会话流图失败(%s): %s", path, exc)

    def sync_from_canvas(self, file_path: str) -> Dict[str, Any]:
        """画布保存 session .grc 后: version+1, Claim 失效, 标记 canvas_dirty。"""
        path = os.path.abspath(file_path or "")
        session_path = os.path.abspath(self._state.project.grc_path or "")
        if not path or not session_path or path != session_path:
            return {"ok": False, "skipped": True}
        semantic_hash = _flowgraph_semantic_hash(path)
        prior_hash = str(
            self._state.project.config.get("flowgraph_semantic_hash") or ""
        )
        if semantic_hash and semantic_hash == prior_hash:
            return {"ok": True, "skipped": True, "unchanged": True}
        self._state.project.flowgraph_version += 1
        self._state.project.config["canvas_dirty"] = True
        self._state.project.config["rf_armed"] = False
        self._state.project.config.pop("rf_armed_path", None)
        if semantic_hash:
            self._state.project.config["flowgraph_semantic_hash"] = semantic_hash
        ClaimStore(self._state).invalidate_by_version(
            self._state.project.flowgraph_version
        )
        self._tool_ctx = None
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("SharedState 落盘失败: %s", exc)
            return {"ok": False, "error": str(exc)}
        self._workflow.invalidate(
            "flowgraph_changed", self._state.project.flowgraph_version
        )
        return {
            "ok": True,
            "version": self._state.project.flowgraph_version,
            "claims": ClaimStore(self._state).summary(),
            "spec_digest": self._state.spec_digest(),
            "canvas_dirty": True,
            "workflow_digest": self._workflow.digest(),
        }

    def bind_opened_project(self, file_path: str) -> Dict[str, Any]:
        """Bind a canvas File→Open / reset path into SharedState."""
        path = os.path.abspath(file_path or "")
        if not path or not os.path.isfile(path) or not path.endswith(".grc"):
            return {"ok": False, "skipped": True}
        prior = str(self._state.project.grc_path or "")
        semantic_hash = _flowgraph_semantic_hash(path)
        self._state.project.grc_path = path
        if semantic_hash:
            self._state.project.config["flowgraph_semantic_hash"] = semantic_hash
        self._state.project.config["slot_source"] = "canvas"
        if not prior:
            if int(self._state.project.flowgraph_version or 0) < 1:
                self._state.project.flowgraph_version = 1
        elif os.path.abspath(prior) != path:
            self._state.project.flowgraph_version += 1
        self._tool_ctx = None
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("绑定画布工程失败: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "grc_path": path,
            "version": self._state.project.flowgraph_version,
        }

    def clear_opened_project(self) -> None:
        self._state.project.grc_path = ""
        self._state.project.config.pop("flowgraph_semantic_hash", None)
        self._state.project.config.pop("slot_source", None)
        self._tool_ctx = None
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("清除画布工程失败: %s", exc)

    def restore_last_snapshot(self) -> Dict[str, Any]:
        """把 SharedState 与 .grc 回滚到最近一次改图前快照。"""
        from ..state import restore_snapshot

        snaps = list(self._state.coordination.snapshots or [])
        if not snaps:
            snap_dir = _store.snapshots_dir(self.session_id)
            if os.path.isdir(snap_dir):
                snaps = sorted(
                    os.path.join(snap_dir, name)
                    for name in os.listdir(snap_dir)
                    if name.startswith("v")
                )
        if not snaps:
            return {"ok": False, "error": "There is no snapshot to restore"}
        target = snaps[-1]
        try:
            restored = restore_snapshot(
                target, _store.state_path(self.session_id))
        except Exception as exc:  # noqa: BLE001
            logger.exception("restore_snapshot 失败")
            return {"ok": False, "error": str(exc)}
        self._state = restored
        self._tool_ctx = None
        self._workflow.invalidate(
            "snapshot_restored", restored.project.flowgraph_version
        )
        return {
            "ok": True,
            "snapshot": target,
            "grc_path": restored.project.grc_path,
            "version": restored.project.flowgraph_version,
            "claims": ClaimStore(restored).summary(),
            "spec_digest": restored.spec_digest(),
            "workflow_digest": self._workflow.digest(),
        }

    def archive_workflow(self) -> str:
        """Archive only the active control-plane file for GUI reset."""
        from .hardware_runtime import RUNTIME

        stopped = RUNTIME.stop(self.session_id, emergency=True)
        self._state.project.config["rf_armed"] = False
        self._state.project.config.pop("rf_armed_path", None)
        if stopped.get("run_id"):
            self._state.project.config["runtime"] = {
                **dict(self._state.project.config.get("runtime") or {}),
                **{
                    key: stopped.get(key)
                    for key in (
                        "run_id", "return_code", "reason", "crashed",
                        "stopped_at",
                    )
                    if key in stopped
                },
                "running": False,
                "status": "crashed" if stopped.get("crashed") else "stopped",
            }
        self._state.save(_store.state_path(self.session_id))
        if not stopped.get("already_stopped"):
            _store.append_session_event(
                self.session_id, "hardware_emergency_stop", stopped
            )
        return _store.archive_workflow(self.session_id)

    def record_profile_choice(
        self, *, adaptive: bool, pinned: Optional[str] = None
    ) -> None:
        """GUI pin/unpin. Auto inference never writes Intent, Plan, or tool args."""
        before = self.profile.level
        self.ctx.adaptive = bool(adaptive)
        if pinned:
            self.profile.pin(str(pinned))
            source = "user_pin"
        else:
            self.profile.unpin()
            source = "user_unpin"
        after = self.profile.level
        if self._tool_ctx is not None:
            self._tool_ctx.extra["profile_snapshot"] = after
        if after != before or source == "user_pin":
            _store.append_session_event(
                self.session_id,
                "profile_changed",
                self._workflow_event_payload({
                    "before": before,
                    "after": after,
                    "source": source,
                    "pinned": self.profile.pinned,
                }),
            )

    def step(self, user_text: str, recipe: str = "",
             simulate: bool = True) -> AgentReply:
        """Consume one user Turn and run autonomous Stages to a boundary."""
        reply = self._step_once(
            user_text, recipe=recipe, simulate=simulate, consume_turn=True
        )
        return self._continue_autonomous(reply, recipe=recipe, simulate=simulate)

    def _step_once(
        self,
        user_text: str,
        recipe: str = "",
        simulate: bool = True,
        *,
        consume_turn: bool,
    ) -> AgentReply:
        """Execute at most one Stage without inventing internal user Turns."""
        if getattr(self._state, "_load_failed", False):
            backup = getattr(self._state, "_corrupt_backup", "")
            return self._error_reply(
                "The session SharedState is corrupted. Writes were stopped to protect the original data. "
                f"Backup: {backup or 'creation failed'}"
            )
        if consume_turn:
            _store.append_session_event(
                self.session_id,
                "user_turn_received",
                self._workflow_event_payload({"text": user_text}),
            )
            active = bool(
                self._workflow.workflow is not None
                and self._workflow.workflow.execution_status
                not in ("completed", "errored")
            )
            if not active or self._alignment.needs_alignment():
                try:
                    aligned = self._alignment.consume_text(user_text)
                    self._state.save(_store.state_path(self.session_id))
                except SemanticUnderstandingError as exc:
                    logger.warning("Semantic understanding unavailable: %s", exc)
                    _store.append_session_event(
                        self.session_id,
                        "semantic_understanding_blocked",
                        self._workflow_event_payload({
                            "error_type": type(exc).__name__,
                            "state_preserved": True,
                        }),
                    )
                    return self._semantic_retry_reply()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Intent alignment 失败")
                    return self._error_reply(f"Intent alignment failed: {exc}")
                if aligned.pending or aligned.intent is None:
                    return self._alignment_waiting_reply(aligned.message)
                try:
                    workflow = self._workflow.instantiate(
                        aligned.intent, self._state
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Workflow instantiate 失败")
                    return self._error_reply(f"Workflow creation failed: {exc}")
            else:
                workflow = None
            intent_before_turn = dict(self._state.intent.parameters or {})
            try:
                workflow = workflow or self._workflow.consume_turn(
                    user_text, self._state
                )
            except SemanticUnderstandingError as exc:
                logger.warning("Semantic workflow update unavailable: %s", exc)
                return self._semantic_retry_reply()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Workflow consume_turn 失败")
                return self._error_reply(f"Workflow state error: {exc}")
            if active and workflow is not None:
                impact = analyze_intent_patch(
                    intent_before_turn,
                    dict(workflow.intent.slots or {}),
                    runtime_active=str(self._state.runtime.status or "") == "running",
                )
                if impact.get("requires_stop"):
                    from .hardware_runtime import RUNTIME

                    stopped = RUNTIME.stop(self.session_id, emergency=True)
                    self._state.project.config["rf_armed"] = False
                    self._state.project.config.pop("rf_armed_path", None)
                    _store.append_session_event(
                        self.session_id,
                        "runtime_stopped_for_intent_patch",
                        self._workflow_event_payload(stopped),
                    )
                if impact.get("requires_reconfirmation") and workflow.intent.turn_relation in {
                    "adjustment", "feedback", "answer"
                }:
                    aligned = self._alignment.request_patch_confirmation(
                        workflow.intent, impact
                    )
                    self._state.save(_store.state_path(self.session_id))
                    return self._alignment_waiting_reply(aligned.message)
            if getattr(self.ctx, "adaptive", True):
                try:
                    before_level = self.profile.level
                    self.profile.observe(user_text)
                    after_level = self.profile.level
                    if after_level != before_level:
                        _store.append_session_event(
                            self.session_id,
                            "profile_changed",
                            self._workflow_event_payload({
                                "before": before_level,
                                "after": after_level,
                                "source": "adaptive_text_signals",
                                "signals": (
                                    self.profile.history[-1].get("signals")
                                    if self.profile.history else {}
                                ),
                            }),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("profile.observe 失败,忽略: %s", exc)
        else:
            workflow = self._workflow.workflow
            if workflow is None:
                return self._error_reply("There is no active workflow")
        self._sync_workflow_intent_to_state()
        stage_text = user_text or workflow.intent.raw_text
        design_text = str(workflow.intent.raw_text or "") or stage_text
        ctx = self._make_ctx()
        self._refresh_mutation_gate(ctx, user_text, workflow)
        ctx.extra["user_text"] = design_text
        ctx.extra["workflow_digest"] = self._workflow.digest()
        if workflow.execution_status == "completed" and workflow.outcome == "cancelled":
            try:
                from ..tools.state_tools import resolve_confirmation_decision
                resolve_confirmation_decision(ctx, approved=False)
                self._state.save(_store.state_path(self.session_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("取消 Workflow 时清理旧 Policy pending 失败: %s", exc)
            return self._workflow_cancelled_reply()
        if (
            not consume_turn
            and workflow.execution_status in ("completed", "errored")
        ):
            return AgentReply(
                text="",
                stage="DELIVER",
                done=workflow.execution_status == "completed",
                claims=ClaimStore(self._state).summary(),
                spec_digest=self._state.spec_digest(),
                workflow_digest=self._digest_with_timeline(),
            )
        current = self._workflow.current_stage()
        if current and current.execution_status == "waiting":
            try:
                self._state.save(_store.state_path(self.session_id))
            except OSError as exc:
                logger.warning("等待态 SharedState 落盘失败: %s", exc)
            return self._workflow_waiting_reply()
        stage = self._workflow.start_stage()
        if stage is None:
            return self._error_reply("The workflow has no executable stage")
        if stage.execution_status == "waiting":
            try:
                self._state.save(_store.state_path(self.session_id))
            except OSError as exc:
                logger.warning("等待态 SharedState 落盘失败: %s", exc)
            return self._workflow_waiting_reply()
        ctx.extra["workflow"] = workflow.to_dict()
        ctx.extra["stage_id"] = stage.id
        task_card = _stage_executor.make_task_card(
            workflow, stage, self._state, stage_text
        )
        self._state.coordination.active_task = task_card
        ctx.extra["task_card"] = vars(task_card)
        _store.append_session_event(
            self.session_id, "task_card_created", self._workflow_event_payload({
                "target_agent": task_card.target_agent,
                "task_id": task_card.task_id,
                "stage_id": task_card.stage_id,
            })
        )
        try:
            from ..tools.state_tools import resolve_confirmation_decision

            semantics = self._turn_semantics(workflow)
            decision = str(semantics.get("confirmation_decision") or "none") \
                if user_text else "none"
            resolution = (
                resolve_confirmation_decision(
                    ctx, approved=decision == "approved"
                )
                if decision in {"approved", "rejected"}
                else {"ok": True, "resolved": False}
            )
            if decision in {"approved", "rejected"}:
                # Confirmation is a turn event, not a durable authorization.
                # Consume it once so autonomous continuation cannot approve a
                # later checkpoint without another explicit user decision.
                semantics["confirmation_decision"] = "none"
                workflow.intent.context["turn_semantics"] = semantics
            if resolution.get("resolved") and not resolution.get("approved"):
                self._state.save(_store.state_path(self.session_id))
                self._workflow.finish("cancelled")
                reply = AgentReply(
                    text="The pending project change was cancelled.",
                    stage="CANCELLED",
                    claims=ClaimStore(self._state).summary(),
                    spec_digest=self._state.spec_digest(),
                )
                _store.append_session_event(
                    self.session_id, "confirmation_cancelled", {}
                )
                reply.workflow_digest = self._workflow.digest()
                return reply
            if resolution.get("resolved") and resolution.get("approved"):
                pending = list(self._state.coordination.pending_confirmations)
                last = pending[-1] if pending else {}
                if last.get("action") == "design_link" and last.get("recipe"):
                    reply = self._run_stage_deterministic(
                        ctx, user_text, str(last.get("recipe") or ""),
                        simulate, stage.id)
                    self._finish_workflow_reply(reply)
                    try:
                        self._state.save(_store.state_path(self.session_id))
                    except OSError as exc:
                        logger.warning("SharedState 落盘失败: %s", exc)
                    return reply
            target_recipe, already = (
                self._semantic_recipe_switch(workflow)
                if user_text else ("", "")
            )
            if (
                not target_recipe
                and already
                and not ctx.extra.get("mutation_forbidden")
            ):
                try:
                    self._state.save(_store.state_path(self.session_id))
                except OSError as exc:
                    logger.warning("SharedState 落盘失败: %s", exc)
                reply = AgentReply(
                    text="The current project already uses {}; no recipe change is needed.".format(already),
                    stage="DELIVER",
                    claims=ClaimStore(self._state).summary(),
                    spec_digest=self._state.spec_digest(),
                )
                _store.append_session_event(
                    self.session_id, "recipe_switch_noop",
                    {"already": already},
                )
                self._workflow.finish("passed")
                reply.workflow_digest = self._workflow.digest()
                return reply
            planning_recipe_change = bool(
                target_recipe
                and workflow.task_type == "MODIFY_PROJECT"
                and stage.id == "inspect_and_plan"
            )
            if (
                target_recipe
                and not planning_recipe_change
                and not ctx.extra.get("mutation_forbidden")
            ):
                from ..tools.design_link import design_link

                proposed = design_link(
                    ctx, profile=self.profile, intent=user_text,
                    recipe=target_recipe, simulate=False, render=False,
                )
                if proposed.get("policy") in ("PROPOSE", "CONFIRM"):
                    try:
                        self._state.save(_store.state_path(self.session_id))
                    except OSError as exc:
                        logger.warning("SharedState 落盘失败: %s", exc)
                    _store.append_session_event(
                        self.session_id, "recipe_switch_propose",
                        {
                            "recipe": target_recipe,
                            "from_recipe": proposed.get("from_recipe"),
                            "policy": proposed.get("policy"),
                        },
                    )
                    reply = self._pending_confirm_reply(proposed)
                    self._finish_workflow_reply(reply, ok=True, outcome="passed")
                    return reply
        except Exception as exc:  # noqa: BLE001
            logger.warning("规格提取失败，继续执行原链路: %s", exc)
        reply = self._execute_stage(ctx, stage, design_text, recipe, simulate)
        self._finish_workflow_reply(reply)
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("SharedState 落盘失败: %s", exc)
        return reply

    def _execute_stage(
        self, ctx: ToolContext, stage: Any, stage_text: str, recipe: str, simulate: bool
    ) -> AgentReply:
        if (
            str(getattr(stage, "interaction", "") or "") == "checkpoint"
            and not getattr(stage, "resume_pending", False)
        ):
            if stage.execution_status != "waiting":
                self._workflow._activate_current()
            return self._workflow_waiting_reply()
        if stage.id in _HOST_CONTROLLED_STAGES or self._prefer_host_stage(stage, recipe):
            return self._run_stage_deterministic(
                ctx, stage_text, recipe, simulate, stage.id
            )
        covering = None
        if "flowgraph_saved" in (stage.completion or []):
            from ..knowledge.recipes import covering_recipe

            covering = covering_recipe(
                stage_text,
                list(
                    self._workflow.workflow.intent.capabilities
                    if self._workflow.workflow else []
                ),
                recipe,
            )
        if covering is not None:
            return self._run_stage_deterministic(
                ctx, stage_text, covering.name, simulate, stage.id
            )
        agent = None
        try:
            agent = _orch.build_agent(ctx, stage=stage)
        except Exception as exc:  # noqa: BLE001
            logger.warning("组装 deepagents 失败,改走确定性骨架: %s", exc)
        try:
            if agent is not None:
                return self._run_deep(agent, ctx, stage_text)
            return self._run_stage_deterministic(
                ctx, stage_text, recipe, simulate, stage.id
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("编排执行异常")
            recovered = self._recover_partial_stage(ctx, exc)
            if recovered is not None:
                return recovered
            return self._error_reply(
                f"Orchestration error: {type(exc).__name__}: {exc}"
            )

    def _prefer_host_stage(self, stage: Any, recipe: str) -> bool:
        """Host handlers win for inspect/diagnose/measure and known recipe apply."""
        capabilities = set(
            self._workflow.workflow.intent.capabilities
            if self._workflow.workflow else []
        )
        # A hardware artifact is a hard contract, not a suggestion to the LLM.
        # Keep build/change stages on the deterministic control plane so a
        # failed SDR builder cannot be "recovered" with a File-Sink recipe.
        if (
            "hardware_configure" in capabilities
            and stage.id in {
                "build_and_verify", "tx_build_and_validate",
                "rx_build_and_verify", "apply_and_verify",
            }
        ):
            return True
        if stage.id in {
            "inspect_and_plan",
            "inspect_and_diagnose",
            "inspect_and_measure",
        }:
            return True
        if stage.id != "apply_and_verify":
            return False
        from ..knowledge.recipes import get_recipe

        slots = (
            self._workflow.workflow.intent.slots
            if self._workflow.workflow else {}
        )
        target = str(slots.get("target_recipe") or recipe or "")
        return get_recipe(target) is not None

    def _continue_autonomous(
        self, first: AgentReply, *, recipe: str, simulate: bool
    ) -> AgentReply:
        """Drive following autonomous Stages without reclassifying text."""
        reply = first
        for _ in range(MAX_AUTONOMOUS_STAGES_PER_TURN):
            workflow = self._workflow.workflow
            if workflow is None or workflow.execution_status in (
                "completed", "errored", "waiting"
            ):
                return reply
            if reply.stage == "ERROR" and not reply.done:
                return reply
            if (
                reply.stage == "CRITIC"
                and not reply.done
                and workflow.execution_status != "pending"
            ):
                return reply
            stage = self._workflow.current_stage()
            if stage is None or stage.execution_status == "waiting":
                return reply
            if stage.execution_status not in ("pending", "invalidated"):
                return reply
            previous = reply
            reply = self._step_once(
                "", recipe=recipe, simulate=simulate, consume_turn=False
            )
            self._merge_reply_history(reply, previous)
        reply.stage = "CRITIC"
        reply.needs_confirmation = False
        reply.text = (
            (reply.text + "\n") if reply.text else ""
        ) + "Automatic stage advancement reached its safety limit. Execution stopped and the current state was preserved."
        return reply

    @staticmethod
    def _merge_reply_history(current: AgentReply, previous: AgentReply) -> None:
        artifacts = dict(previous.artifacts or {})
        artifacts.update(current.artifacts or {})
        current.artifacts = artifacts
        current.tool_invocations = list(previous.tool_invocations or []) + list(
            current.tool_invocations or []
        )
        previous_text = (previous.text or "").strip()
        current_text = (current.text or "").strip()
        if previous_text and current_text and previous_text not in current_text:
            current.text = previous_text + "\n" + current_text
        elif previous_text and not current_text:
            current.text = previous_text

    def step_command(self, command: Dict[str, Any]) -> AgentReply:
        """Structured GUI command entry; text remains a compatibility transport."""
        action = str((command or {}).get("action") or "")
        if action == "specification_update":
            runtime = dict(self._state.project.config.get("runtime") or {})
            if runtime.get("running"):
                from .hardware_runtime import RUNTIME

                stopped = RUNTIME.stop(self.session_id, emergency=True)
                self._state.project.config["rf_armed"] = False
                self._state.project.config.pop("rf_armed_path", None)
                _store.append_session_event(
                    self.session_id,
                    "runtime_stopped_for_specification_update",
                    self._workflow_event_payload(stopped),
                )
            try:
                aligned = self._alignment.consume_updates(command)
                self._state.save(_store.state_path(self.session_id))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Radio Specification 更新失败")
                return self._error_reply(f"Specification update failed: {exc}")
            if aligned.pending or aligned.intent is None:
                return self._alignment_waiting_reply(aligned.message)
            try:
                self._workflow.instantiate(aligned.intent, self._state)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Workflow instantiate 失败")
                return self._error_reply(f"Workflow creation failed: {exc}")
            reply = self._step_once(
                "", recipe="", simulate=True, consume_turn=False
            )
            return self._continue_autonomous(reply, recipe="", simulate=True)
        if action == "interaction_response":
            try:
                aligned = self._alignment.consume_response(command)
                self._state.save(_store.state_path(self.session_id))
            except Exception as exc:  # noqa: BLE001
                logger.exception("Structured intent interaction 失败")
                return self._error_reply(f"Intent interaction failed: {exc}")
            if aligned.pending or aligned.intent is None:
                return self._alignment_waiting_reply(aligned.message)
            try:
                self._workflow.instantiate(aligned.intent, self._state)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Workflow instantiate 失败")
                return self._error_reply(f"Workflow creation failed: {exc}")
            reply = self._step_once(
                "", recipe="", simulate=True, consume_turn=False
            )
            return self._continue_autonomous(reply, recipe="", simulate=True)
        if action == "retry_transmit":
            return self._retry_transmit()
        if action in {"stop_runtime", "emergency_stop"}:
            return self._stop_runtime_command(emergency=action == "emergency_stop")
        if action == "retry_stage":
            return self._retry_waiting_stage()
        if action == "cancel_workflow":
            return self._cancel_waiting_workflow()
        if action != "checkpoint_decision":
            return self._error_reply(f"Unknown GUI command: {action or '(empty)'}")
        stage = self._workflow.current_stage()
        checkpoint = stage.checkpoint if stage else None
        checkpoint_id = str(command.get("checkpoint_id") or "")
        if not checkpoint or checkpoint.id != checkpoint_id:
            return self._error_reply("The checkpoint has changed. Refresh and try again.")
        if checkpoint.blocker:
            reply = self._workflow_waiting_reply()
            reply.stage = "WAITING"
            reply.needs_confirmation = False
            reply.text = "{}{}".format(
                checkpoint.blocker.get("message") or "The required system capability is unavailable.",
                (
                    "\n" + str(checkpoint.blocker.get("remediation") or "")
                    if checkpoint.blocker.get("remediation") else ""
                ),
            )
            return reply
        decision = str(command.get("decision") or "")
        if decision not in ("approved", "rejected"):
            return self._error_reply("The checkpoint decision must be approved or rejected.")
        _store.append_session_event(
            self.session_id,
            "checkpoint_command_received",
            self._workflow_event_payload(
                {"checkpoint_id": checkpoint_id, "decision": decision,
                 "purpose": checkpoint.purpose}
            ),
        )
        stage_id = stage.id if stage else ""
        self._state.decisions.append(WorkflowDecision(
            decision_id=f"decision-{uuid.uuid4().hex[:8]}",
            key=f"checkpoint:{checkpoint.purpose}",
            value=decision,
            source="gui",
            effect_level=str(checkpoint.requested_effect or "READ"),
            workflow_id=str(self._workflow.workflow.workflow_id),
            stage_id=stage_id,
        ))
        if decision == "approved":
            effect = str(checkpoint.requested_effect or "READ")
            if is_rf_grant_effect(effect) and checkpoint.purpose != "rf_authorization":
                return self._error_reply(
                    "Only an RF authorization checkpoint can grant RF_RUN."
                )
            if effect not in self._state.runtime.granted_effects:
                self._state.runtime.granted_effects.append(effect)
        if stage_id == "rf_plan_confirmation":
            slots = self._workflow.workflow.intent.slots
            effect = str(checkpoint.requested_effect or "")
            rf_plan = {
                "status": decision,
                "checkpoint_id": checkpoint_id,
                "purpose": checkpoint.purpose,
                "device": dict(
                    self._state.project.config.get("observed_device") or {}
                ),
                "center_frequency": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
                "bandwidth": slots.get("bandwidth") or slots.get("sample_rate"),
                "tx_gain": slots.get("tx_gain"),
                "tx_attenuation": slots.get("tx_attenuation"),
            }
            if is_rf_grant_effect(effect):
                rf_plan["max_duration_seconds"] = (
                    slots.get("max_duration_seconds")
                    or slots.get("duration_seconds")
                    or 30.0
                )
            self._state.project.config["rf_plan"] = rf_plan
            self._record_claim(
                "rf_plan_decision_recorded",
                "User decision is bound to the typed RF plan checkpoint",
                "hardware",
                "rf_plan_confirmation",
                dict(self._state.project.config["rf_plan"]),
                True,
            )
        if stage_id in ("over_air_verification", "runtime_observation"):
            key = (
                "over_air_observed"
                if stage_id == "over_air_verification"
                else "runtime_observed"
            )
            if stage_id == "over_air_verification":
                from ..tools import registry

                ctx = self._make_ctx()
                status = registry.call("query_runtime_status", {}, ctx)
                _store.append_session_event(
                    self.session_id,
                    "tool_called",
                    self._workflow_event_payload({
                        "tool": "query_runtime_status",
                        "origin": registry.origin_of("query_runtime_status"),
                        "runtime": registry.runtime_of("query_runtime_status"),
                        "args": {},
                        "result": status,
                    }),
                )
                observation = dict(command.get("observation") or {})
                expected_name = str(
                    self._workflow.workflow.intent.slots.get("local_name") or ""
                )
                observed_name = str(observation.get("observed_name") or "")
                now = float(observation.get("observed_at") or time.time())
                within_window = bool(
                    status.get("running")
                    and status.get("ready")
                    and status.get("run_id")
                    and now <= float(status.get("deadline") or 0)
                )
                if decision == "approved" and not within_window:
                    reply = self._workflow_waiting_reply()
                    reply.stage = "CRITIC"
                    reply.text = (
                        "The bounded transmission is not within a valid runtime window, so over-the-air verification cannot pass. "
                        "Inspect runtime errors and run the bounded transmission again."
                    )
                    return reply
                if decision == "approved" and observed_name != expected_name:
                    reply = self._workflow_waiting_reply()
                    reply.stage = "CRITIC"
                    reply.text = (
                        f"The observed over-the-air name does not match the target: expected {expected_name or '(empty)'}, "
                        f"received {observed_name or '(not provided)'}."
                    )
                    return reply
                attached = _store.attach_evidence(
                    self.session_id,
                    str(observation.get("artifact") or ""),
                    run_id=str(status.get("run_id") or ""),
                )
                if attached:
                    try:
                        _store.write_artifact_manifest(
                            self.session_id,
                            {"evidence": attached.get("artifact") or ""},
                        )
                    except OSError as exc:
                        logger.debug("写入 Evidence Manifest 失败: %s", exc)
                    _store.append_session_event(
                        self.session_id,
                        "evidence_attached",
                        self._workflow_event_payload({
                            "run_id": status.get("run_id"),
                            "evidence_id": attached.get("path") or "",
                            "sha256": attached.get("sha256") or "",
                            "size": attached.get("size") or 0,
                        }),
                    )
                artifact = str(attached.get("path") or "")
                evidence_kind = str(
                    observation.get("evidence_kind")
                    or ("screenshot" if artifact else "human_confirmation")
                )
                ota_observation = {
                    "observed": decision == "approved",
                    "observed_name": observed_name,
                    "expected_name": expected_name,
                    "observed_at": now,
                    "run_id": status.get("run_id"),
                    "evidence_kind": evidence_kind,
                    "artifact": artifact,
                    "sha256": attached.get("sha256") or "",
                    "evidence_id": attached.get("path") or "",
                }
                self._workflow.workflow.intent.slots["ota_observation"] = ota_observation
                self._workflow.workflow.intent.slots[key] = decision == "approved"
                self._record_ota_observation(
                    decision == "approved", "gui_checkpoint", ota_observation
                )
            else:
                self._workflow.workflow.intent.slots[key] = decision == "approved"
        try:
            from ..tools.state_tools import resolve_confirmation_decision

            ctx = self._make_ctx()
            resolve_confirmation_decision(
                ctx, approved=decision == "approved"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("同步 Policy confirmation 失败: %s", exc)
        self._workflow.resolve_checkpoint(decision)
        self._sync_control_state()
        self._state.save(_store.state_path(self.session_id))
        if self._workflow.workflow.execution_status in ("completed", "errored"):
            if self._workflow.workflow.outcome == "cancelled":
                return self._workflow_cancelled_reply()
            return AgentReply(
                text="The current workflow has already ended.",
                stage="DELIVER",
                done=True,
                claims=ClaimStore(self._state).summary(),
                spec_digest=self._state.spec_digest(),
                artifacts=self._current_grc_artifacts(),
                workflow_digest=self._digest_with_timeline(),
            )
        reply = self._step_once(
            "", recipe="", simulate=True, consume_turn=False
        )
        return self._continue_autonomous(reply, recipe="", simulate=True)

    def _retry_waiting_stage(self) -> AgentReply:
        workflow = self._workflow.workflow
        stage = self._workflow.current_stage()
        if (
            workflow is None
            or stage is None
            or workflow.execution_status != "waiting"
        ):
            return self._error_reply("There is no stage available to retry.")
        blocker = dict(self._state.runtime.blocker or {})
        if blocker and not blocker.get("retryable", False):
            reply = self._workflow_waiting_reply()
            reply.needs_confirmation = False
            reply.text = "{}{}".format(
                blocker.get("message") or "The current blocker cannot be retried directly.",
                "\n" + str(blocker.get("remediation") or "")
                if blocker.get("remediation") else "",
            )
            return reply
        # A retry must observe the CURRENT external state.  When the workflow
        # involves hardware, re-discover and re-probe the device first so a
        # reconnected SDR is actually picked up instead of replaying the old
        # failure with stale inputs.
        probe_note = self._refresh_hardware_for_retry()
        if probe_note.startswith("SDR_NOT_FOUND:"):
            reply = self._workflow_waiting_reply()
            reply.stage = "WAITING"
            reply.needs_confirmation = False
            reply.text = probe_note.split("SDR_NOT_FOUND:", 1)[1]
            return reply
        stage.execution_status = "pending"
        stage.outcome = ""
        stage.resume_pending = True
        workflow.execution_status = "pending"
        self._workflow.save()
        reply = self._step_once("", recipe="", simulate=True, consume_turn=False)
        reply = self._continue_autonomous(reply, recipe="", simulate=True)
        if probe_note and probe_note not in (reply.text or ""):
            reply.text = "{}\n{}".format(reply.text, probe_note).strip()
        return reply

    def _refresh_hardware_for_retry(self) -> str:
        """Re-check the SDR before a retry; stash evidence for the next stage.

        Returns an empty string when the workflow does not involve hardware,
        a ``SDR_NOT_FOUND:``-prefixed message when nothing was detected, or a
        short confirmation note when the device was found and probed.
        """
        workflow = self._workflow.workflow
        if workflow is None:
            return ""
        capabilities = set(workflow.intent.capabilities or [])
        if not capabilities & {"hardware_configure", "deploy", "hardware_runtime"}:
            return ""
        from ..tools import registry

        ctx = self._tool_ctx or self._make_ctx()
        discovery = registry.call("discover_devices", {}, ctx)
        self._record_tool_result(ctx, "discover_devices", discovery, {})
        devices = list(discovery.get("devices") or [])
        found = bool(discovery.get("device_found") or devices)
        identity = ""
        if found:
            slots = dict(workflow.intent.slots or {})
            probe_args = {"device_type": slots.get("hardware") or "sdr"}
            if devices:
                first = devices[0] if isinstance(devices[0], dict) else {}
                identity = str(
                    first.get("device_identity") or first.get("uri") or ""
                )
                probe_args["uri"] = identity
            probe = registry.call("probe_device", probe_args, ctx)
            self._record_tool_result(
                ctx, "probe_device", probe, probe_args
            )
            if probe.get("device_probed"):
                identity = str(
                    probe.get("device_identity") or identity
                )
            found = bool(probe.get("device_probed", found))
        # Keep the fresh evidence for the re-run stage and mirror it into
        # shared state (observed_device) so identity checks see it too.
        self._retry_preflight_events = [
            item for item in (ctx.extra.get("events") or [])
            if item.get("kind") in ("discover_devices", "probe_device")
        ]
        scratch = AgentReply()
        scratch.tool_invocations = [
            ToolInvocation(
                name=str(item.get("kind") or ""),
                args=dict(item.get("args") or {}),
                result=item.get("payload") or {},
                ok=bool(isinstance(item.get("payload"), dict) and item["payload"].get("ok", True)),
            )
            for item in self._retry_preflight_events
        ]
        try:
            self._project_tool_results(self._workflow.current_stage(), scratch)
        except Exception as exc:  # noqa: BLE001
            logger.debug("retry 预检查结果投影失败,忽略: %s", exc)
        if not found:
            return (
                "SDR_NOT_FOUND:No SDR was detected just now. "
                "Connect the device (and wait for it to enumerate), then press "
                "Retry — I will re-check it automatically."
            )
        device = str(discovery.get("device_type") or "")
        if devices and isinstance(devices[0], dict):
            device = str(devices[0].get("device_type") or device)
        return "Re-checked the SDR before retrying: {} {} detected and probed.".format(
            device or "SDR", identity,
        ).replace("  ", " ")

    def _cancel_waiting_workflow(self) -> AgentReply:
        if self._workflow.workflow is None:
            return self._error_reply("There is no active workflow.")
        self._workflow.finish("cancelled")
        return self._workflow_cancelled_reply()

    def _retry_transmit(self) -> AgentReply:
        stage = self._workflow.current_stage()
        if stage is None or stage.id not in (
            "over_air_verification", "runtime_observation", "transmit_bounded",
            "run_bounded",
        ):
            return self._error_reply("The current stage cannot retry a bounded transmission.")
        from ..tools import registry

        ctx = self._make_ctx()
        if self._workflow.workflow:
            ctx.extra["workflow"] = self._workflow.workflow.to_dict()
            ctx.extra["stage_id"] = stage.id
        ctx.extra["force_hardware_start"] = True
        slots = self._workflow.workflow.intent.slots if self._workflow.workflow else {}
        start_args = {
            "grc_path": self._state.project.grc_path,
            "duration_seconds": slots.get("max_duration_seconds")
            or slots.get("duration_seconds")
            or 30.0,
        }
        result = registry.call("start_flowgraph", start_args, ctx)
        self._record_tool_result(ctx, "start_flowgraph", result, start_args)
        max_duration = start_args["duration_seconds"]
        reply = self._fold(
            ctx,
            result.get("error")
            or (
                f"Bounded transmission retry started (maximum duration: {max_duration:g} seconds; "
                "OTA confirmation or cancellation stops it early). "
                "You do not need to click Run in GRC."
            ),
            source="deterministic-stage",
            ok=bool(result.get("running") and result.get("ready")),
        )
        self._project_tool_results(stage, reply)
        self._finish_workflow_reply(reply, ok=bool(result.get("running")))
        return reply

    def _stop_runtime_command(self, *, emergency: bool) -> AgentReply:
        """Always-available host stop; it does not advance the Workflow."""
        from ..tools import registry

        ctx = self._make_ctx()
        name = "emergency_stop" if emergency else "stop_flowgraph"
        result = registry.call(name, {}, ctx)
        self._record_tool_result(ctx, name, result, {})
        self._state.project.config["rf_armed"] = False
        self._state.project.config.pop("rf_armed_path", None)
        self._state.runtime.granted_effects = [
            effect for effect in self._state.runtime.granted_effects
            if not is_rf_grant_effect(effect)
        ]
        self._state.runtime.status = "stopped"
        self._state.save(_store.state_path(self.session_id))
        reply = self._fold(
            ctx,
            ("Emergency stop completed." if emergency else "The current runtime was stopped."),
            source="host-runtime-control",
            ok=bool(result.get("ok", True)),
        )
        reply.stage = "RUNTIME"
        reply.spec_digest = self._state.spec_digest()
        reply.claims = ClaimStore(self._state).summary()
        reply.workflow_digest = self._digest_with_timeline()
        return reply

    def _run_deep(self, agent: Any, ctx: ToolContext,
                  user_text: str) -> AgentReply:
        ctx.extra["execution_mode"] = "deepagents"
        workflow = dict(ctx.extra.get("workflow") or {})
        task_card = dict(ctx.extra.get("task_card") or {})
        thread_id = ":".join(
            str(item)
            for item in (
                self.session_id,
                workflow.get("workflow_id") or "workflow",
                workflow.get("revision") or 0,
                ctx.extra.get("stage_id") or "stage",
                task_card.get("task_id") or "attempt",
            )
        )
        config = {"configurable": {"thread_id": thread_id},
                  "recursion_limit": _recursion_limit()}
        try:
            task_json = json.dumps(
                ctx.extra.get("task_card") or {}, ensure_ascii=False, separators=(",", ":")
            )
            result = agent.invoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                f"{user_text}\n\nCurrent Stage TaskCard: {task_json}\n\n"
                                "Return all user-facing narrative in English. "
                                "Write the final answer as a concise, friendly message for the user: "
                                "first what was accomplished, then the key results with units, "
                                "then what happens next or what you need from them. "
                                "Do not mention tool names, tool-call success or failure, "
                                "internal field names, JSON, or session bookkeeping."
                            ),
                        }
                    ]
                },
                config,
            )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "GraphRecursionError":
                logger.warning("编排达到步数上限,按已产出结果交付: %s", exc)
                _store.append_session_event(
                    self.session_id, "recursion_limit",
                    {"limit": config["recursion_limit"]})
                done = bool(ctx.extra.get("artifacts", {}).get("grc_path")) or bool(
                    self._state.project.grc_path)
                note = ("The orchestration step limit was reached; further exploration was stopped."
                        if done else "The orchestration step limit was reached before a usable flowgraph was produced.")
                return self._fold(ctx, note, source="deepagents-truncated",
                                  ok=done)
            recovered = self._recover_partial_stage(ctx, exc)
            if recovered is not None:
                return recovered
            raise

        narrative = self._extract_final_text(result)
        self._record_deep_delegations(result, ctx)
        files = result.get("files") if isinstance(result, dict) else None
        if files:
            _store.mirror_session_files(self.session_id, files)
        return self._fold(ctx, narrative, source="deepagents")

    def _recover_partial_stage(
        self, ctx: ToolContext, exc: BaseException
    ) -> AgentReply | None:
        timeout = "timeout" in type(exc).__name__.lower()
        artifacts = dict(ctx.extra.get("artifacts") or {})
        try:
            final = _store.scan_final_artifacts(self.session_id)
        except Exception:  # noqa: BLE001
            final = {}
        for name, path in (final or {}).items():
            if str(name).endswith(".grc"):
                artifacts.setdefault("grc_path", path)
            artifacts.setdefault(name, path)
        if artifacts:
            ctx.extra["artifacts"] = artifacts
        if not timeout and not artifacts.get("grc_path"):
            return None
        has_grc = bool(artifacts.get("grc_path"))
        note = (
            "Orchestration timed out; the generated flowgraph was preserved."
            if timeout and has_grc
            else "Orchestration was interrupted ({}).".format(type(exc).__name__)
        )
        return self._fold(
            ctx, note, source="deepagents-partial", ok=has_grc
        )

    def _record_deep_delegations(self, result: Any, ctx: ToolContext) -> None:
        if not isinstance(result, dict):
            return
        calls_by_id = {}
        for message in result.get("messages") or []:
            calls = getattr(message, "tool_calls", None)
            if calls is None and isinstance(message, dict):
                calls = message.get("tool_calls")
            for call in calls or []:
                name = call.get("name") if isinstance(call, dict) else ""
                args = call.get("args") if isinstance(call, dict) else {}
                if name == "task":
                    call_id = str(call.get("id") or f"call-{uuid.uuid4().hex[:8]}")
                    target = str((args or {}).get("subagent_type") or "")
                    parent = self._state.coordination.active_task
                    invocation = (
                        vars(_stage_executor.make_invocation_card(parent, target))
                        if parent and target
                        else {"task_id": call_id, "target_agent": target}
                    )
                    invocation["call_id"] = call_id
                    calls_by_id[call_id] = invocation
                    ctx.extra.setdefault("subagent_invocations", []).append(invocation)
                    _store.append_session_event(
                        self.session_id,
                        "llm_subagent_invoked",
                        self._workflow_event_payload({
                            "target_agent": target,
                            "description": (args or {}).get("description"),
                            "task_id": invocation.get("task_id"),
                            "call_id": call_id,
                            "mode": "llm",
                            "executor": "deepagents",
                        }),
                    )
        for message in result.get("messages") or []:
            call_id = getattr(message, "tool_call_id", None)
            content = getattr(message, "content", None)
            if call_id is None and isinstance(message, dict):
                call_id = message.get("tool_call_id")
                content = message.get("content")
            if call_id not in calls_by_id:
                continue
            parsed = self._parse_result_envelope(content)
            invocation = calls_by_id[call_id]
            parent = self._state.coordination.active_task
            _stage_executor.bind_invocation_result(invocation, parsed, parent)
            _store.append_session_event(
                self.session_id,
                "subagent_completed"
                if invocation.get("protocol_valid")
                else "result_envelope_invalid",
                self._workflow_event_payload(
                    {
                        "target_agent": invocation.get("target_agent"),
                        "task_id": invocation.get("task_id"),
                        "call_id": call_id,
                        "result": parsed,
                    }
                ),
            )

    @staticmethod
    def _parse_result_envelope(content: Any) -> Dict[str, Any]:
        if isinstance(content, dict):
            return dict(content)
        if not isinstance(content, str):
            return {}
        text = content.strip()
        candidates = [text]
        if "```" in text:
            candidates.extend(
                part.strip().removeprefix("json").strip()
                for part in text.split("```")[1::2]
            )
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _extract_final_text(result: Any) -> str:
        """从 deepagents 返回的 state 里取最后一条 AI 消息文本。"""
        if not isinstance(result, dict):
            return ""
        msgs = result.get("messages") or []
        for m in reversed(msgs):
            content = getattr(m, "content", None)
            if content is None and isinstance(m, dict):
                content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):  # 多段 content
                parts = [p.get("text", "") for p in content
                         if isinstance(p, dict)]
                joined = "".join(parts).strip()
                if joined:
                    return joined
        return ""

    # ---- 降级路径:确定性 design_link --------------------------------
    def _run_deterministic(self, ctx: ToolContext, user_text: str,
                           recipe: str, simulate: bool) -> AgentReply:
        from ..tools.design_link import design_link
        if not recipe:
            pending = self._state.coordination.pending_confirmations
            if pending and pending[-1].get("approved"):
                recipe = str(pending[-1].get("recipe") or "")
        result = design_link(ctx, profile=self.profile, intent=user_text,
                             recipe=recipe, simulate=simulate, render=True)

        # 环境级失败(缺 platform/gnuradio):如实报告,不伪装成建图结果。
        # DENY 没有 recipe 字段,但不能当成环境不可用。
        if (
            not result.get("ok")
            and "recipe" not in result
            and str(result.get("policy") or "").upper() != "DENY"
        ):
            err = result.get("error", "Flowgraph build environment unavailable")
            _store.append_session_event(self.session_id, "env_error",
                                        {"error": err})
            return self._error_reply(f"Flowgraph build environment unavailable: {err}")

        # 把 design_link 结果并入 ctx,复用统一折叠
        _merge(ctx.extra.setdefault("artifacts", {}),
               result.get("artifacts", {}))
        if result.get("metrics"):
            ctx.extra.setdefault("metrics", {}).update(result["metrics"])
        ctx.extra["events"].append({"kind": "design_link", "payload": {
            "ok": result.get("ok"),
            "recipe": result.get("recipe"), "valid": result.get("valid"),
            "steps": result.get("steps", []),
            "policy": result.get("policy"),
            "error": result.get("error")}})
        narrative = (
            result.get("narrative")
            or result.get("error")
            or self._fallback_text(result)
        )
        return self._fold(ctx, narrative, source="deterministic",
                          ok=bool(result.get("ok")))

    def _run_stage_deterministic(
        self,
        ctx: ToolContext,
        user_text: str,
        recipe: str,
        simulate: bool,
        stage_id: str,
    ) -> AgentReply:
        from .stage_handlers import run_deterministic_stage

        return run_deterministic_stage(
            self, ctx, user_text, recipe, simulate, stage_id
        )

    def _hardware_rx_spectrum_ready(self) -> bool:
        intent = self._workflow.workflow.intent if self._workflow.workflow else None
        if intent is None:
            return False
        capabilities = set(intent.capabilities or [])
        slots = dict(intent.slots or {})
        return (
            "signal_agnostic_observe" in capabilities
            and slots.get("direction") == "rx"
            and bool(slots.get("hardware"))
            and slots.get("carrier_frequency") is not None
            and slots.get("sample_rate") is not None
        )

    def _run_hardware_rx_spectrum(self, ctx: ToolContext) -> AgentReply:
        from ..tools import registry

        slots = self._workflow.workflow.intent.slots if self._workflow.workflow else {}
        result = registry.call(
            "build_sdr_rx_spectrum_flowgraph",
            {
                "device_type": slots.get("hardware"),
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
                "device_args": str(
                    (self._state.project.config.get("observed_device") or {}).get(
                        "identity"
                    )
                    or device_args_for(str(slots.get("hardware") or ""))
                ),
            },
            ctx,
        )
        self._record_tool_result(ctx, "build_sdr_rx_spectrum_flowgraph", result)
        if result.get("grc_path"):
            ctx.extra.setdefault("artifacts", {})["grc_path"] = result["grc_path"]
        validation = self._validate_loaded(ctx)
        self._record_tool_result(ctx, "validate_flowgraph", validation)
        note = result.get("error") or (
            "Generated a live receive-spectrum flowgraph for the selected SDR (QT GUI Frequency Sink). "
            "RF has not started; the live spectrum will appear in a GNU Radio QT window, not as a PNG in the conversation."
            if result.get("ok")
            else "Unable to generate the B210 receive-spectrum flowgraph."
        )
        return self._fold(
            ctx, note, source="deterministic-stage",
            ok=bool(result.get("ok")) and bool(validation.get("valid")),
        )

    def _run_hardware_endpoint_flowgraph(self, ctx: ToolContext) -> AgentReply:
        from ..tools import registry

        intent = self._workflow.workflow.intent if self._workflow.workflow else None
        slots = dict(intent.slots or {}) if intent else {}
        if str(slots.get("direction") or "") == "rx":
            return self._fold(
                ctx,
                "No deterministic template can currently provide verified coverage for this receive-hardware endpoint. Execution stopped for more information.",
                source="deterministic-stage",
                ok=False,
            )
        if self._matching_unarmed_tx_preview(slots):
            self._load_session_flowgraph(ctx)
            validation = registry.call("validate_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "validate_flowgraph", validation)
            path = str(self._state.project.grc_path or "")
            ctx.extra.setdefault("artifacts", {})["grc_path"] = path
            reused = {
                "ok": bool(validation.get("valid")),
                "valid": bool(validation.get("valid")),
                "reused_preview": True,
                "grc_path": path,
                "preview_mode": self._state.project.config.get("preview_mode"),
                "armed": False,
                "not_started": True,
            }
            self._record_tool_result(ctx, "build_sdr_tx_flowgraph", reused)
            return self._fold(
                ctx,
                self._tx_preview_note(slots, reused=True),
                source="deterministic-stage",
                ok=bool(validation.get("valid")),
            )
        result = registry.call(
            "build_sdr_tx_flowgraph",
            {
                "device_type": slots.get("hardware") or "sdr",
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
            },
            ctx,
        )
        self._record_tool_result(ctx, "build_sdr_tx_flowgraph", result)
        if result.get("grc_path"):
            ctx.extra.setdefault("artifacts", {})["grc_path"] = result["grc_path"]
        return self._fold(
            ctx,
            result.get("error") or self._tx_preview_note(slots, reused=False),
            source="deterministic-stage",
            ok=bool(result.get("ok")) and bool(result.get("valid")),
        )

    def _matching_unarmed_tx_preview(self, slots: Dict[str, Any]) -> bool:
        config = dict(self._state.project.config or {})
        path = str(self._state.project.grc_path or "")
        if not path or not os.path.isfile(path):
            return False
        if bool(config.get("rf_armed")) or str(config.get("direction") or "") != "tx":
            return False
        requested = normalize_hardware(slots.get("hardware") or "")
        existing = normalize_hardware(config.get("hardware") or "")
        if requested and existing and requested != existing:
            return False
        for key, cfg_key in (
            ("carrier_frequency", "carrier_frequency"),
            ("sample_rate", "sample_rate"),
        ):
            if slots.get(key) in (None, "") or config.get(cfg_key) in (None, ""):
                continue
            try:
                left = float(slots[key])
                right = float(config[cfg_key])
            except (TypeError, ValueError):
                return False
            if abs(left - right) > max(1.0, abs(right) * 1e-9):
                return False
        return True

    @staticmethod
    def _tx_preview_note(slots: Dict[str, Any], *, reused: bool) -> str:
        tone = (
            " A quiet 1 kHz test tone stands in as the signal."
            if not slots.get("modulation") else ""
        )
        head = (
            "Reusing the transmit flowgraph you already saved."
            if reused
            else "Your transmit flowgraph is ready in safe preview mode."
        )
        if slots.get("operation") == "deploy":
            tail = " Nothing will be transmitted until you approve it."
        else:
            tail = " Nothing is being transmitted — I'll wait for your confirmation before anything runs."
        return f"{head}{tone}{tail}"

    def _validate_loaded(self, ctx: ToolContext) -> Dict[str, Any]:
        from ..tools import registry

        return registry.call(
            "validate_flowgraph", {"arm_disabled_rf": True}, ctx
        )

    @staticmethod
    def _flowgraph_sample_rate(ctx: ToolContext) -> float:
        """Read ``samp_rate`` from the loaded GRC; default 1 Msps for baseband recipes."""
        block = (getattr(ctx, "blocks", None) or {}).get("samp_rate")
        if block is None:
            return 1e6
        try:
            value = (getattr(block, "params", None) or {})["value"].get_value()
            rate = float(str(value).strip().replace("'", "").replace('"', ""))
        except (AttributeError, KeyError, TypeError, ValueError):
            return 1e6
        return rate if rate > 0 else 1e6

    def _record_tool_result(
        self,
        ctx: ToolContext,
        kind: str,
        result: Dict[str, Any],
        args: Optional[Dict[str, Any]] = None,
    ) -> None:
        from .tools_lc import record_tool_event

        record_tool_event(ctx, kind, dict(result or {}), args)

    def _finish_workflow_reply(
        self,
        reply: AgentReply,
        *,
        ok: Optional[bool] = None,
        outcome: str = "",
    ) -> None:
        stage = self._workflow.current_stage()
        pending_items = list(self._state.coordination.pending_confirmations or [])
        unresolved_pending = next(
            (item for item in reversed(pending_items) if not item.get("approved")),
            None,
        )
        if (
            stage
            and stage.execution_status in ("running", "errored")
            and (reply.stage == "CONFIRM" or unresolved_pending)
        ):
            pending = unresolved_pending or dict(reply.pending or {})
            checkpoint = self._workflow.wait_for_checkpoint(
                str(pending.get("reason") or pending.get("action") or "Waiting for user confirmation"),
                action=str(pending.get("action") or "workflow_checkpoint"),
                payload_ref=str(pending.get("id") or ""),
            )
            if unresolved_pending is not None:
                unresolved_pending.setdefault("id", f"pending-{uuid.uuid4().hex[:8]}")
                unresolved_pending["checkpoint_id"] = checkpoint.id
                checkpoint.payload_ref = unresolved_pending["id"]
                self._workflow.save()
                reply.pending = dict(unresolved_pending)
            self._state.coordination.active_task = None
            reply.stage = "CONFIRM"
            reply.needs_confirmation = True
            reply.done = False
            reply.workflow_digest = self._digest_with_timeline()
            return
        if stage and stage.execution_status in ("running", "errored"):
            self._project_stage_effects(stage, reply)
            self._project_tool_results(stage, reply)
            active = self._state.coordination.active_task
            if active is None:
                active = _stage_executor.make_task_card(
                    self._workflow.workflow, stage, self._state, ""
                )
            invocations = list(
                (self._tool_ctx.extra if self._tool_ctx else {}).get(
                    "subagent_invocations"
                )
                or []
            )
            if not invocations:
                deep_mode = bool(
                    self._tool_ctx
                    and self._tool_ctx.extra.get("execution_mode") == "deepagents"
                )
                if (not deep_mode) or reply.tool_invocations:
                    invocations = _stage_executor.synthesize_deterministic_invocations(
                        active,
                        stage,
                        reply,
                        executor_id=(
                            "main_agent" if deep_mode
                            else "deterministic_stage_handler"
                        ),
                    )
                if self._tool_ctx is not None:
                    self._tool_ctx.extra["subagent_invocations"] = invocations
                for item in invocations:
                    _store.append_session_event(
                        self.session_id,
                        "executor_completed",
                        self._workflow_event_payload(
                            {
                                "target_agent": item.get("target_agent"),
                                "task_id": item.get("task_id"),
                                "mode": "main_agent" if deep_mode else "deterministic",
                                "protocol_valid": True,
                                "result": item.get("result"),
                            }
                        ),
                    )
            envelope = _stage_executor.make_result_envelope(
                self._workflow.workflow,
                stage,
                self._state,
                active,
                reply,
                invocations,
            )
            missing_completion = [
                name for name, passed in envelope.completion.items() if not passed
            ]
            note = ""
            if not envelope.ok and reply.stage not in ("ERROR", "DENY", "CONFIRM"):
                reply.needs_confirmation = False
                failure_codes = list(envelope.acceptance.get("failure_codes") or [])
                external_only = bool(missing_completion) and all(
                    name in EXTERNAL_PRECONDITION_COMPLETIONS
                    for name in missing_completion
                )
                if external_only:
                    # Engine parks the stage as waiting; the reply mirrors
                    # that state instead of a hard failure verdict.
                    note = external_waiting_note(missing_completion)
                    reply.stage = "WAITING"
                    reply.text = "{}\n{}".format(reply.text, note).strip()
                else:
                    note = _friendly_stage_failure(
                        missing_completion, failure_codes
                    )
                    reply.stage = "CRITIC"
                    reply.text = "{}\n{}".format(reply.text, note).strip()
            result = vars(envelope)
            result["errored"] = reply.stage == "ERROR"
            if not envelope.ok and missing_completion:
                # Digest/monitor show this note as the waiting reason.
                result["note"] = note
            if getattr(stage, "resume_from", ""):
                result["resume_from"] = stage.resume_from
            result["improvement_available"] = any(
                invocation.name in ("explain_error", "debug_by_metric")
                for invocation in reply.tool_invocations
            )
            if reply.stage == "DENY":
                result["ok"] = False
                result["outcome"] = outcome or "failed"
            elif ok is not None and not bool(ok):
                result["ok"] = False
                result["outcome"] = outcome or "failed"
            self._workflow.accept_result(result)
            self._state.coordination.active_task = None
            self._workflow.workflow.base_project_version = int(
                self._state.project.flowgraph_version
            )
            self._workflow.save()
        current = self._workflow.current_stage()
        if current and current.execution_status == "waiting" and current.checkpoint:
            capability_blocker = dict(current.checkpoint.blocker or {})
            pending_items = list(self._state.coordination.pending_confirmations or [])
            unresolved = next(
                (item for item in reversed(pending_items) if not item.get("approved")),
                None,
            )
            if unresolved is not None:
                unresolved.setdefault("id", f"pending-{uuid.uuid4().hex[:8]}")
                unresolved["checkpoint_id"] = current.checkpoint.id
                unresolved["requested_effect"] = current.checkpoint.requested_effect
                unresolved["purpose"] = current.checkpoint.purpose
            reply.stage = "WAITING" if capability_blocker else "CONFIRM"
            reply.needs_confirmation = not bool(capability_blocker)
            if unresolved is not None:
                reply.pending = dict(unresolved)
            elif not reply.pending:
                slots = (
                    self._workflow.workflow.intent.slots
                    if self._workflow.workflow else {}
                )
                reply.pending = {
                    "action": current.id,
                    "reason": current.checkpoint.reason,
                    "checkpoint_id": current.checkpoint.id,
                    "stage_id": current.id,
                    "requested_effect": current.checkpoint.requested_effect,
                    "purpose": current.checkpoint.purpose,
                    "approved": False,
                }
                if is_rf_grant_effect(current.checkpoint.requested_effect):
                    reply.pending["max_duration_seconds"] = (
                        slots.get("max_duration_seconds")
                        or slots.get("duration_seconds")
                        or 30.0
                    )
            if current.id == "rf_plan_confirmation":
                slots = (
                    self._workflow.workflow.intent.slots
                    if self._workflow.workflow else {}
                )
                observed = dict(
                    self._state.project.config.get("observed_device") or {}
                )
                rf_plan = {
                    "status": (
                        "blocked" if capability_blocker else "awaiting_user"
                    ),
                    "checkpoint_id": current.checkpoint.id,
                    "purpose": current.checkpoint.purpose,
                    "device": observed,
                    "center_frequency": slots.get("carrier_frequency"),
                    "sample_rate": slots.get("sample_rate"),
                    "bandwidth": slots.get("bandwidth") or slots.get("sample_rate"),
                    "tx_gain": slots.get("tx_gain"),
                    "tx_attenuation": slots.get("tx_attenuation"),
                    "baseband_kind": slots.get("baseband_kind"),
                    "tone_frequency_hz": slots.get("tone_frequency_hz"),
                    "tone_amplitude": slots.get("tone_amplitude"),
                }
                if is_rf_grant_effect(current.checkpoint.requested_effect):
                    rf_plan["max_duration_seconds"] = (
                        slots.get("max_duration_seconds")
                        or slots.get("duration_seconds")
                        or 30.0
                    )
                self._state.project.config["rf_plan"] = rf_plan
                reply.pending.update(rf_plan)
                if not is_rf_grant_effect(current.checkpoint.requested_effect):
                    reply.pending.pop("max_duration_seconds", None)
                reply.pending.update({
                    "action": (
                        "capability_blocker"
                        if capability_blocker else "rf_plan_confirmation"
                    ),
                    "reason": (
                        capability_blocker.get("message")
                        or current.checkpoint.reason
                        or (
                            "Confirm the device identity, RF parameters, and bounded runtime"
                            if is_rf_grant_effect(current.checkpoint.requested_effect)
                            else "Confirm the device identity and RF parameters; confirmation will not start RF"
                        )
                    ),
                    "requested_effect": current.checkpoint.requested_effect,
                    "purpose": current.checkpoint.purpose,
                    "blocker": capability_blocker,
                    "can_confirm": not bool(capability_blocker),
                    "can_retry": bool(
                        capability_blocker.get("retryable", False)
                    ),
                    "approved": False,
                })
                if capability_blocker.get("remediation"):
                    reply.text = "{}\n{}".format(
                        capability_blocker.get("message") or reply.text,
                        capability_blocker["remediation"],
                    )
        reply.workflow_digest = self._digest_with_timeline()
        reply.done = reply.workflow_digest.get("execution_status") == "completed"
        reply.claims = ClaimStore(self._state).summary()
        reply.spec_digest = self._state.spec_digest()
        if not (reply.artifacts or {}).get("grc_path"):
            reply.artifacts = {
                **dict(reply.artifacts or {}),
                **self._current_grc_artifacts(),
            }

    def _project_stage_effects(self, stage: Any, reply: AgentReply) -> None:
        """Commit host-observed artifacts for deterministic and LLM executors."""
        from ..workflow.completion import MUTATING_TOOLS, invocation_succeeded

        grc_path = str((reply.artifacts or {}).get("grc_path") or "")
        if not grc_path:
            return
        successful_tools = {
            item.name
            for item in (reply.tool_invocations or [])
            if invocation_succeeded(item)
        }
        if not successful_tools.intersection(MUTATING_TOOLS):
            return
        workflow = self._workflow.workflow
        self._state.project.grc_path = grc_path
        semantic_hash = _flowgraph_semantic_hash(grc_path)
        old_hash = str(
            self._state.project.config.get("flowgraph_semantic_hash") or ""
        )
        if old_hash and semantic_hash == old_hash:
            return
        if semantic_hash:
            self._state.project.config["flowgraph_semantic_hash"] = semantic_hash
        if workflow and self._state.project.flowgraph_version <= int(
            workflow.base_project_version
        ):
            self._state.project.flowgraph_version += 1
        if workflow:
            slots = workflow.intent.slots
            for key in (
                "modulation", "channel", "protocol", "local_name", "hardware",
                "carrier_frequency", "sample_rate", "duration_seconds",
                "signal_source_scope",
            ):
                value = slots.get(key)
                if value not in (None, "", []):
                    self._state.project.config[key] = value
            target_recipe = str(slots.get("target_recipe") or "")
            if target_recipe:
                self._state.project.config["recipe"] = target_recipe
            for key in ("modulation", "channel"):
                value = slots.get(key)
                if value in (None, "", []):
                    continue
                existing = next(
                    (item for item in self._state.spec.decisions if item.key == key),
                    None,
                )
                if existing is None:
                    self._state.spec.decisions.append(
                        Decision(key=key, value=value, source="apply")
                    )
                else:
                    existing.value = value
                    existing.source = "apply"
            hardware = str(slots.get("hardware") or "")
            if hardware:
                self._state.project.config["desired_device"] = {
                    "type": hardware,
                    "center_freq": slots.get("carrier_frequency"),
                    "sample_rate": slots.get("sample_rate"),
                }

    def _project_tool_results(self, stage: Any, reply: AgentReply) -> None:
        _projector.project_tool_results(
            self._state,
            reply,
            record_claim=self._record_claim,
            semantic_hash=_flowgraph_semantic_hash,
        )
        if self._workflow.workflow is not None:
            self._workflow.workflow.quality = self._state.runtime.quality

    def _record_claim(
        self,
        claim_id: str,
        statement: str,
        layer: str,
        test: str,
        observation: Dict[str, Any],
        passed: bool,
        artifact: str = "",
        *,
        producer: str = "",
        measurement_id: str = "",
        evidence_grade: str = "system_verified",
    ) -> None:
        store = ClaimStore(self._state)
        version = int(self._state.project.flowgraph_version)
        mid = str(
            measurement_id
            or observation.get("measurement_id")
            or ""
        )
        store.upsert(Claim(
            id=claim_id,
            statement=statement,
            layer=layer,
            status="NotTested",
            project_version=version,
            producer=producer,
            measurement_id=mid,
        ))
        store.add_evidence(
            claim_id,
            Evidence(
                test=test,
                observation=dict(observation or {}),
                project_version=version,
                artifact=artifact,
                measurement_id=mid,
                evidence_grade=evidence_grade,
            ),
            passed=passed,
        )

    def _record_ota_observation(
        self,
        observed: bool,
        source: str,
        observation: Optional[Dict[str, Any]] = None,
    ) -> None:
        slots = self._workflow.workflow.intent.slots if self._workflow.workflow else {}
        details = dict(observation or {})
        details.setdefault("observed", observed)
        details.setdefault("local_name", slots.get("local_name"))
        details.setdefault("evidence_kind", "human_confirmation")
        artifact = str(details.get("artifact") or "")
        details["evidence_complete"] = bool(artifact)
        evidence_grade = "attached_capture" if artifact else "human_statement"
        details["evidence_grade"] = evidence_grade
        requested_name = str(slots.get("local_name") or "")
        claim_id = (
            "ota_ble_local_name_observed"
            if requested_name else "ota_target_signal_observed"
        )
        statement = (
            "External receiver observed the requested BLE Complete Local Name"
            if requested_name
            else "External receiver observed the requested signal"
        )
        self._record_claim(
            claim_id,
            statement,
            "hardware",
            source,
            details,
            observed,
            artifact=artifact,
            producer="over_air_verification",
            evidence_grade=evidence_grade,
        )

    def _current_grc_artifacts(self) -> Dict[str, str]:
        path = str(getattr(self._state.project, "grc_path", "") or "")
        if path and os.path.isfile(path):
            return {"grc_path": path}
        return {}

    def _digest_with_timeline(self) -> Dict[str, Any]:
        self._sync_control_state()
        digest = self._workflow.digest()
        shared_intent = self._state.intent
        digest["shared_intent"] = {
            **shared_intent.snapshot(),
            "interaction": dict(shared_intent.interaction or {}),
        }
        digest["project_version"] = int(
            self._state.project.flowgraph_version or 0
        )
        diagnosis = self._state.diagnosis
        if (
            diagnosis.intent_id
            and diagnosis.intent_id == shared_intent.intent_id
            and diagnosis.intent_revision == shared_intent.revision
        ):
            digest["diagnosis"] = {
                "intent_id": diagnosis.intent_id,
                "intent_revision": diagnosis.intent_revision,
                "requested_dimensions": list(diagnosis.requested_dimensions),
                "findings": list(diagnosis.findings),
                "summary": dict(diagnosis.summary),
                "report_path": diagnosis.report_path,
                "created_at": diagnosis.created_at,
            }
        if self._alignment.needs_alignment():
            interaction = dict(shared_intent.interaction or {})
            digest.update({
                "task_type": shared_intent.task_type or "INTENT_ALIGNMENT",
                "task_label": "Intent Alignment",
                "execution_status": "waiting",
                "current_stage": "intent_alignment",
                "stage_label": "Intent Recognition and Parameter Alignment",
                "stage_index": 0,
                "stage_total": 0,
                "wait_kind": "input",
                "waiting_reason": interaction.get("prompt") or "Waiting for additional intent information",
                "interaction_request": interaction,
                "needs_confirmation": True,
                "can_confirm": True,
                "workflow_id": "wf-" + shared_intent.intent_id.removeprefix("intent-"),
                "revision": shared_intent.revision,
                "capabilities": list(shared_intent.capabilities),
                "missing_slots": list(shared_intent.missing_fields),
                "validation_errors": list(shared_intent.validation_errors),
            })
        digest["timeline"] = _store.recent_events(self.session_id, limit=40)
        runtime = dict(self._state.project.config.get("runtime") or {})
        if runtime:
            deadline = float(runtime.get("deadline") or 0)
            runtime["remaining_seconds"] = max(0.0, deadline - time.time()) \
                if runtime.get("running") and deadline else 0.0
            log_path = _store.resolve_session_path(
                self.session_id, str(runtime.get("log_path") or "")
            )
            if not log_path:
                candidate = os.path.join(
                    _store.session_root(self.session_id),
                    "final", "hardware_runtime", "runtime.log",
                )
                log_path = candidate if os.path.isfile(candidate) else ""
            runtime["log_tail"] = _read_log_tail(log_path)
            if log_path:
                runtime["log_path"] = log_path
            current = digest.get("current_stage") or ""
            runtime["can_retry"] = (
                current in {
                    "over_air_verification", "runtime_observation",
                }
                and not runtime.get("running")
            )
            runtime["do_not_run_grc"] = True
            digest["runtime"] = runtime
        digest["control_state"] = {
            "current_node": self._state.runtime.current_node,
            "status": self._state.runtime.status,
            "requested_effect": self._state.runtime.requested_effect,
            "granted_effects": list(self._state.runtime.granted_effects),
            "blocker": dict(self._state.runtime.blocker),
            "quality": self._state.runtime.quality,
            "warnings": list(self._state.runtime.warnings),
        }
        return digest

    def _alignment_waiting_reply(self, message: str = "") -> AgentReply:
        interaction = dict(self._state.intent.interaction or {})
        self._state.spec.open_questions = list(dict.fromkeys(
            list(self._state.intent.missing_fields)
            + list(self._state.intent.validation_errors)
        ))
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("意图等待态 SharedState 落盘失败: %s", exc)
        text = str(message or interaction.get("prompt") or "Please provide the missing intent information.")
        return AgentReply(
            text=text,
            stage="ALIGN",
            needs_confirmation=True,
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            pending=interaction,
            artifacts=self._current_grc_artifacts(),
            workflow_digest=self._digest_with_timeline(),
        )

    def _semantic_retry_reply(self) -> AgentReply:
        """Expose an LLM outage without inventing a rule-based interpretation."""
        return AgentReply(
            text=(
                "⚠️ I couldn't reach the language model, so your message was not "
                "interpreted — nothing was changed. Please check the model "
                "connection (endpoint, key, network) and send the message again."
            ),
            stage="ALIGN",
            done=False,
            needs_confirmation=False,
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            pending={},
            artifacts=self._current_grc_artifacts(),
            workflow_digest=self._digest_with_timeline(),
        )

    def peek_runtime_digest(self) -> Dict[str, Any]:
        """Refresh hardware runtime for GUI polling without advancing Workflow."""
        if self._tool_ctx is None:
            return self._digest_with_timeline()
        try:
            from ..tools.hardware_tools import query_runtime_status

            query_runtime_status(self._tool_ctx)
        except Exception as exc:  # noqa: BLE001
            logger.debug("peek runtime 失败: %s", exc)
        return self._digest_with_timeline()

    def _workflow_waiting_reply(self) -> AgentReply:
        stage = self._workflow.current_stage()
        intent = self._workflow.workflow.intent if self._workflow.workflow else None
        missing = list(getattr(intent, "missing_slots", None) or [])
        validation_errors = list(
            getattr(intent, "validation_errors", None) or []
        )
        if missing:
            labels = {
                "modulation": "Specify the modulation scheme, such as BPSK, QPSK, or OFDM.",
                "current_project": "There is no project to inspect. Build or open a .grc project first.",
                "hardware": "Specify the SDR device type, such as USRP B210.",
                "carrier_frequency": "Specify the center frequency, such as 2.4 GHz.",
                "sample_rate": "Specify the sample rate, such as 1 MHz.",
                "local_name": "Specify the BLE Complete Local Name to advertise.",
                "ebn0_db": "Specify the Eb/N0 for the BER simulation, such as 8 dB.",
            }
            text = "\n".join(labels.get(item, f"Please provide {item}.") for item in missing)
            pending = {}
        elif validation_errors:
            labels = {
                "carrier_frequency_invalid": "The center-frequency format or value is invalid. Specify it again.",
                "carrier_frequency_out_of_device_range": "The center frequency is outside the selected device's supported range. Specify it again.",
                "sample_rate_invalid": "The sample rate must be positive.",
                "bandwidth_invalid": "The bandwidth must be positive.",
                "symbol_rate_invalid": "The symbol rate must be positive.",
            }
            text = "\n".join(
                labels.get(item, f"Parameter validation failed: {item}.")
                for item in validation_errors
            )
            pending = {}
        elif stage and stage.checkpoint and stage.checkpoint.blocker:
            blocker = dict(stage.checkpoint.blocker)
            pending = {
                "action": "capability_blocker",
                "reason": blocker.get("message") or "The required system capability is unavailable.",
                "blocker": blocker,
                "can_confirm": False,
                "can_retry": bool(blocker.get("retryable", False)),
                "requires_restart": bool(blocker.get("requires_restart", False)),
                "approved": False,
            }
            text = "{}{}".format(
                pending["reason"],
                "\n" + str(blocker.get("remediation") or "")
                if blocker.get("remediation") else "",
            )
        elif stage and stage.checkpoint:
            pending_items = list(self._state.coordination.pending_confirmations or [])
            pending = dict(pending_items[-1]) if pending_items else {
                "action": (
                    "over_air_verification"
                    if stage.id == "over_air_verification"
                    else "rf_plan_confirmation"
                    if stage.id == "rf_plan_confirmation"
                    else "workflow_checkpoint"
                ),
                "reason": (
                    stage.checkpoint.reason if stage and stage.checkpoint
                    else stage_display_label(
                        stage.id if stage else "",
                        "",
                        stage.checkpoint.requested_effect if stage and stage.checkpoint else "",
                    )
                ),
                "checkpoint_id": stage.checkpoint.id if stage and stage.checkpoint else "",
                "stage_id": stage.id,
                "requested_effect": (
                    stage.checkpoint.requested_effect if stage.checkpoint else ""
                ),
                "approved": False,
            }
            rf_grant = is_rf_grant_effect(
                (stage.checkpoint.requested_effect if stage.checkpoint else "")
                or ""
            )
            text = (
                "Click 'Target Signal Observed' only after an independent receiver has actually observed the target signal. "
                "You may attach a screenshot; confirmation stops the bounded process early."
                if stage.id == "over_air_verification"
                else (
                    "The current stage is waiting for your confirmation. Confirm to continue, or cancel to keep the existing project. "
                    "After approval, the workflow starts transmission automatically; you do not need to click Run in GRC."
                    if rf_grant
                    else "The workflow is paused at a decision boundary. Confirmation does not start RF. "
                    "Explicit runtime authorization is required to transmit."
                )
                if stage.id == "rf_plan_confirmation"
                else "The current stage is waiting for your confirmation. Confirm to continue, or cancel to keep the existing project."
            )
            if stage.id == "rf_plan_confirmation":
                slots = intent.slots if intent else {}
                observed = dict(
                    self._state.project.config.get("observed_device") or {}
                )
                pending.update({
                    "action": "rf_plan_confirmation",
                    "device": observed,
                    "center_frequency": slots.get("carrier_frequency"),
                    "sample_rate": slots.get("sample_rate"),
                    "bandwidth": slots.get("bandwidth") or slots.get("sample_rate"),
                    "tx_gain": slots.get("tx_gain"),
                    "tx_attenuation": slots.get("tx_attenuation"),
                    "baseband_kind": slots.get("baseband_kind"),
                    "tone_frequency_hz": slots.get("tone_frequency_hz"),
                    "tone_amplitude": slots.get("tone_amplitude"),
                })
                if rf_grant:
                    pending["max_duration_seconds"] = (
                        slots.get("max_duration_seconds")
                        or slots.get("duration_seconds")
                        or 30.0
                    )
        else:
            pending = {}
            note = str(
                (getattr(stage, "result", {}) or {}).get("note") or ""
            )
            text = note or (
                "The current stage could not complete. Tell me what to adjust, "
                "or press Retry to run it again."
            )
        return AgentReply(
            text=text,
            stage=(
                "WAITING"
                if stage and stage.checkpoint and stage.checkpoint.blocker
                else "CONFIRM" if stage and stage.checkpoint else "CRITIC"
            ),
            needs_confirmation=bool(
                stage and stage.checkpoint and not stage.checkpoint.blocker
            ),
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            pending=pending,
            artifacts=self._current_grc_artifacts(),
            workflow_digest=self._digest_with_timeline(),
        )

    def _workflow_cancelled_reply(self) -> AgentReply:
        return AgentReply(
            text="The current workflow was cancelled; the existing project remains unchanged.",
            stage="CANCELLED",
            done=True,
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            workflow_digest=self._digest_with_timeline(),
        )

    def _record_metric_claims(self, metrics: Dict[str, Any]) -> None:
        """Persist auditable measurement Claims, not just GUI scalar values."""
        store = ClaimStore(self._state)
        version = int(self._state.project.flowgraph_version)
        for claim_id, statement, report_key in (
            ("evm_measured", "EVM measured from a bound IQ probe", "evm_report"),
            ("ber_measured", "BER measured from bound TX/RX probes", "ber_report"),
            ("spectrum_peak_measured", "Spectrum peak measured with calibrated frequency axis", "spectrum_peak_report"),
        ):
            report = metrics.get(report_key)
            if not isinstance(report, dict) or not report.get("valid"):
                continue
            store.upsert(Claim(
                id=claim_id,
                statement=statement,
                layer="sim",
                project_version=version,
                producer="read_metric",
                measurement_id=str(
                    report.get("measurement_id") or metrics.get("measurement_id") or ""
                ),
            ))
            store.add_evidence(
                claim_id,
                Evidence(
                    test=report_key,
                    observation=dict(report),
                    project_version=version,
                    measurement_id=str(
                        report.get("measurement_id")
                        or metrics.get("measurement_id")
                        or ""
                    ),
                    evidence_grade="system_measurement",
                ),
                passed=True,
            )

    # ---- 统一折叠:ctx -> AgentReply ---------------------------------
    def _fold(self, ctx: ToolContext, narrative: str, *,
              source: str, ok: bool = True) -> AgentReply:
        reply = AgentReply()
        artifacts: Dict[str, str] = dict(ctx.extra.get("artifacts", {}))
        policy_decisions = []
        explicit_confirmation = False
        if artifacts.get("grc_path"):
            artifacts["grc_path"] = _store.publish_artifact(
                self.session_id, artifacts["grc_path"]
            )
            self._state.project.grc_path = artifacts["grc_path"]

        # 把工具事件折叠为 tool_invocations,并落会话事件流
        from ..workflow.completion import tool_payload_succeeded

        for ev in ctx.extra.get("events", []):
            kind = ev.get("kind", "")
            args = dict(ev.get("args") or {})
            payload = ev.get("payload", {})
            if isinstance(payload, dict) and payload.get("policy"):
                policy_decisions.append(payload["policy"])
            if isinstance(payload, dict) and payload.get(
                "requires_confirmation"
            ):
                explicit_confirmation = True
            reply.tool_invocations.append(ToolInvocation(
                name=kind, args=args, result=payload,
                ok=tool_payload_succeeded(payload)))
            if not ev.get("logged"):
                _store.append_session_event(
                    self.session_id,
                    "tool_called",
                    self._workflow_event_payload(
                        {
                            "tool": kind,
                            "origin": ev.get("origin") or "",
                            "runtime": ev.get("runtime") or "",
                            "args": args,
                            "result": payload,
                        }
                    ),
                )

        # DeepAgent 可能只把 .grc 写到磁盘。只补当前缺失的流图，
        # 不要把 final/ 里上一轮工程扫进本轮产物。
        if not artifacts.get("grc_path"):
            for name, path in _store.scan_final_artifacts(self.session_id).items():
                if name.endswith(".grc"):
                    artifacts["grc_path"] = path
                    break

        if ctx.extra.get("metrics"):
            artifacts["metrics"] = ctx.extra["metrics"]
            self._record_metric_claims(ctx.extra["metrics"])

        reply.artifacts = artifacts
        manifest = _store.write_artifact_manifest(self.session_id, artifacts)
        artifacts["manifest"] = manifest
        self._sync_artifact_index(manifest)
        self._state.project.config["artifact_refs"] = {
            key: os.path.relpath(
                value, _store.session_root(self.session_id)
            ).replace(os.sep, "/")
            for key, value in artifacts.items()
            if isinstance(value, str) and os.path.isfile(value)
        }
        export_dir = str(ctx.extra.get("export_dir") or "")
        if export_dir:
            _store.export_session_artifacts(
                self.session_id,
                export_dir,
                [
                    value for value in artifacts.values()
                    if isinstance(value, str) and os.path.isfile(value)
                ],
            )
        state = ctx.extra.get("state")
        if state is not None:
            reply.claims = ClaimStore(state).summary()
            reply.spec_digest = state.spec_digest()
        reply.text = self._render_evidence_text(ctx, narrative, source)
        denied = "DENY" in policy_decisions
        needs_policy_response = explicit_confirmation or any(
            item in ("CONFIRM", "PROPOSE") for item in policy_decisions
        )
        reply.stage = (
            "DENY"
            if denied
            else (
                "CONFIRM"
                if needs_policy_response
                else ("DELIVER" if ok else "CRITIC")
            )
        )
        reply.done = False
        reply.needs_confirmation = False if denied else needs_policy_response
        pending_list = []
        if state is not None:
            pending_list = list(state.coordination.pending_confirmations or [])
        elif self._state.coordination.pending_confirmations:
            pending_list = list(self._state.coordination.pending_confirmations)
        last_pending = pending_list[-1] if pending_list else {}
        if last_pending and not last_pending.get("approved"):
            reply.pending = dict(last_pending)
            reply.needs_confirmation = True
            if reply.stage not in ("DENY", "ERROR"):
                reply.stage = "CONFIRM"
        _store.append_session_event(self.session_id, "reply", self._workflow_event_payload({
            "source": source, "stage": reply.stage,
            "has_grc": bool(artifacts.get("grc_path"))}))
        return reply

    def _pending_confirm_reply(self, result: Dict[str, Any]) -> AgentReply:
        pending = {}
        items = list(self._state.coordination.pending_confirmations or [])
        if items:
            pending = dict(items[-1])
        from_recipe = (
            result.get("from_recipe")
            or pending.get("from_recipe")
            or self._state.project.config.get("recipe")
            or "Current Project"
        )
        to_recipe = result.get("recipe") or pending.get("recipe") or ""
        text = (
            f"The current project will change from '{from_recipe}' to '{to_recipe}'. "
            "The flowgraph will be rebuilt only after confirmation; existing claims will become stale and be revalidated against the new version. "
            "The canvas remains unchanged until you confirm."
        )
        reply = AgentReply(
            text=text,
            stage="CONFIRM",
            needs_confirmation=True,
            pending=pending,
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
        )
        _store.append_session_event(self.session_id, "reply", self._workflow_event_payload({
            "source": "policy-propose", "stage": "CONFIRM", "has_grc": False}))
        return reply

    # ---- 辅助 --------------------------------------------------------
    def _error_reply(self, msg: str) -> AgentReply:
        reply = AgentReply()
        reply.text = msg
        reply.stage = "ERROR"
        reply.done = False
        # An execution error is a recovery boundary, not an approval request.
        # Confirmation controls are valid only when Workflow has a checkpoint.
        reply.needs_confirmation = False
        reply.claims = ClaimStore(self._state).summary()
        reply.spec_digest = self._state.spec_digest()
        reply.workflow_digest = self._digest_with_timeline()
        return reply

    def _sink_engine_event(self, event: str, payload: Dict[str, Any]) -> None:
        data = dict(payload or {})
        data.setdefault("profile_level", getattr(self.profile, "level", "student"))
        _store.append_session_event(self.session_id, event, data)
        if not self._progress_listeners or event not in _PROGRESS_EVENTS:
            return
        try:
            snapshot = {
                "event": str(event or "state_changed"),
                "event_payload": data,
                "workflow_digest": self._digest_with_timeline(),
                "spec_digest": self._state.spec_digest(),
                "claims": ClaimStore(self._state).summary(),
                "pending": dict(self._state.intent.interaction or {}),
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("生成 Workflow progress snapshot 失败: %s", exc)
            return
        for callback in list(self._progress_listeners):
            try:
                callback(snapshot)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Workflow progress listener 失败: %s", exc)

    def _workflow_event_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        workflow = self._workflow.workflow
        stage = self._workflow.current_stage()
        if workflow:
            data.setdefault("workflow_id", workflow.workflow_id)
            data.setdefault("workflow_revision", workflow.revision)
            data.setdefault("task_type", workflow.task_type)
            data.setdefault("stage_id", workflow.current_stage)
            data.setdefault("attempt", stage.attempt if stage else 0)
        data.setdefault("profile_level", getattr(self.profile, "level", "student"))
        return data

    @staticmethod
    def _render_evidence_text(
        ctx: ToolContext, narrative: str, source: str
    ) -> str:
        """Render a user-facing reply: friendly narrative first, compact facts after.

        The narrative (deterministic handler note or LLM answer) leads the
        reply.  Host tool observations stay authoritative but are folded into
        at most a few short lines: what completed, what needs attention,
        saved artifacts and measurement caveats.  Raw per-tool success logs
        are deliberately not replayed to the user.
        """
        events = list(ctx.extra.get("events") or [])
        parts: List[str] = []
        if narrative:
            parts.append(narrative)
        if source.startswith("deterministic"):
            completed = []
            failed = []
            for event in events:
                name = str(event.get("kind") or "")
                payload = event.get("payload") or {}
                label = _DETERMINISTIC_PROGRESS.get(name)
                if not label:
                    continue
                failed_flag = isinstance(payload, dict) and (
                    payload.get("ok") is False or payload.get("valid") is False
                )
                if failed_flag:
                    failed.append(name)
                    continue
                if name == "probe_device":
                    identity = str(
                        payload.get("device_identity") or payload.get("uri") or ""
                    )
                    device = str(payload.get("device_type") or "SDR")
                    label = (
                        f"{device} {identity} detected and probed" if identity
                        else "SDR detected and probed"
                    )
                completed.append(label.lstrip("✓ ").strip())
            if completed:
                parts.append("Completed: " + " · ".join(dict.fromkeys(completed)) + ".")
            if failed:
                parts.append(
                    "These steps did not complete: "
                    + ", ".join(dict.fromkeys(failed)) + "."
                )
        elif source.startswith("deepagents"):
            failed = []
            not_started = False
            for event in events:
                name = str(event.get("kind") or "")
                payload = event.get("payload") or {}
                if not name:
                    continue
                if isinstance(payload, dict) and payload.get("ok") is False:
                    failed.append(name)
                if isinstance(payload, dict) and payload.get("not_started"):
                    not_started = True
            if failed:
                parts.append(
                    "These steps need attention: "
                    + ", ".join(dict.fromkeys(failed)) + "."
                )
            artifacts = [
                os.path.basename(value)
                for value in (ctx.extra.get("artifacts") or {}).values()
                if isinstance(value, str) and value
            ]
            if artifacts:
                parts.append("Saved: " + ", ".join(dict.fromkeys(artifacts)) + ".")
            if not_started:
                parts.append("The flowgraph has not started, so this cannot support a claim of successful RF or over-the-air verification.")
            if not parts:
                return narrative or "The current stage has ended; see the tool results for detailed facts."
        else:
            return narrative or ServiceAgent._fallback_text(
                {"recipe": None, "metrics": ctx.extra.get("metrics")}
            )
        metrics = dict(ctx.extra.get("metrics") or {})
        source_scope = str(metrics.get("signal_source_scope") or "")
        source_labels = {
            "generated_fixture": "Measurement source: self-contained generated fixture (not the current over-the-air signal).",
            "current_project_offline": "Measurement source: offline simulation of the current project (not live reception).",
            "live_device": "Measurement source: the bound live hardware receive path.",
        }
        if source_labels.get(source_scope):
            parts.append(source_labels[source_scope])
        ber = metrics.get("ber_report")
        if isinstance(ber, dict) and ber.get("valid"):
            parts.append(
                "BER={} ({}/{} bits; 95% one-sided upper bound={}; {}).".format(
                    ber.get("value"), ber.get("bit_errors"),
                    ber.get("compared_bits"),
                    ber.get("confidence_upper_bound"),
                    ber.get("alignment_method") or "alignment unavailable",
                )
            )
        return "\n".join(parts)

    @staticmethod
    def _fallback_text(data: dict) -> str:
        recipe = data.get("recipe") or "selected recipe"
        metrics = data.get("metrics") or {}
        parts = [f"Built and validated the flowgraph using '{recipe}'."]
        if metrics.get("evm_pct") is not None:
            parts.append(f" Simulated EVM ≈ {metrics['evm_pct']:.2f}%.")
        return "".join(parts)


def build_service_agent(session_id: Optional[str] = None,
                        profile: Any = None) -> ServiceAgent:
    """便捷构造入口(与参考实现 build_service_agent 命名对齐)。"""
    return ServiceAgent(session_id=session_id, profile=profile)
