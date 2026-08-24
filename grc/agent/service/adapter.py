"""adapter:GUI 适配层 —— deepagents 运行结果 -> AgentReply / .grc 路径。

本层是 DeepAgents 编排层与现有 GUI 之间的**契约守门人**:

* 对外只暴露 :class:`ServiceAgent`,其 :meth:`ServiceAgent.step` 返回现有
  :class:`grc.agent.schema.AgentReply`(GUI 侧渲染逻辑零改动)。
* **主路径**:用 :func:`orchestrator.build_agent` 组装的 deepagents 深度代理运行
  一轮(``invoke``),从共享 ToolContext 收集工具产物与事件,折叠为一次
  AgentReply,并把产物镜像到 ``local/agent_sessions/<id>/final/``。
* **降级路径**(红线 4):未装 deepagents 或未配置 LLM 时,直接跑确定性
  ``design_link`` 宏建图 —— 无 LLM 也能产出 .grc(论文 baseline)。
* 任一路径的异常都收敛为 AgentReply(stage=ERROR)而非崩溃。
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

from ..schema import AgentReply, ToolInvocation
from ..memory.profile import UserProfile
from ..state import Claim, ClaimStore, Decision, Evidence, SharedState
from ..tools.registry import ToolContext
from ..tools.hardware_tools import normalize_sdr_hardware
from ..tools.hardware_profiles import resolve_hardware_profile
from ..workflow import WorkflowEngine
from . import orchestrator as _orch
from . import session_store as _store
from . import stage_executor as _stage_executor

logger = logging.getLogger(__name__)

#: 一轮编排允许的 LangGraph 超步数上限(可用 GRC_AGENT_RECURSION_LIMIT 覆盖)。
#: 6 个 subagent 的闭环路由 + 工具调用远超 50 步,过小会在正常流程中途撞限。
DEFAULT_RECURSION_LIMIT = 150
MAX_AUTONOMOUS_STAGES_PER_TURN = 16

# Safety-critical and deterministic protocol stages are executed by the host
# control plane.  The LLM still creates/routs the Workflow, but cannot omit,
# reorder, or duplicate hardware gates by choosing tools opportunistically.
_HOST_CONTROLLED_STAGES = frozenset({
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
    """DeepRadio 服务级 Agent,对 GUI 暴露 AgentReply 契约。

    Example:
        >>> agent = build_service_agent()
        >>> reply = agent.step("做一个 BPSK over AWGN,看 EVM")
        >>> reply.artifacts.get("grc_path")   # 供 GUI 原地刷新画布
    """

    def __init__(self, session_id: Optional[str] = None,
                 profile: Any = None, platform: Any = None):
        self.session_id = session_id or f"gui-{uuid.uuid4().hex[:8]}"
        # 统一用 memory.profile.UserProfile(创新 B);GUI 通过 ctx.profile 驱动。
        self.profile = profile if isinstance(profile, UserProfile) \
            else UserProfile()
        self._platform = platform
        self._state = SharedState.load(
            _store.state_path(self.session_id), session_id=self.session_id
        )
        self._tool_ctx: Optional[ToolContext] = None
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
        # GUI 兼容层:agent.ctx.{tool_ctx.out_dir, adaptive, profile}
        self.ctx = _CtxShim(self)

    # ---- 上下文装配 --------------------------------------------------
    def _make_ctx(self) -> ToolContext:
        """构造本轮共享 ToolContext(platform + 会话 final 输出目录)。

        Tool 永远写入会话 ``final/``；GUI 指定目录只接收导出副本。这样运行
        白名单、恢复路径和用户可见路径不会指向不同根目录。
        """
        export_dir = (self.ctx.tool_ctx.out_dir or "").strip()
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
        ctx.extra["metrics"] = {}
        ctx.extra["subagent_invocations"] = []
        ctx.extra["export_dir"] = export_dir
        if ctx.flow_graph is None:
            self._load_session_flowgraph(ctx)
        return ctx

    def _sync_workflow_intent_to_state(self) -> None:
        """Project canonical Workflow Intent into RadioSpec without reparsing."""
        workflow = self._workflow.workflow
        if workflow is None:
            return
        intent = workflow.intent
        if intent.raw_text and intent.raw_text not in self._state.spec.goals:
            self._state.spec.goals.append(intent.raw_text)
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
        for key in ("modulation", "channel", "hardware", "protocol"):
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
        self._state.spec.open_questions = list(
            dict.fromkeys(
                list(intent.missing_slots) + list(intent.validation_errors)
            )
        )

    def _load_session_flowgraph(self, ctx: ToolContext) -> None:
        """从会话已保存的 .grc 把内存流图灌进 agent 用的 core Platform。"""
        path = str(self._state.project.grc_path or "")
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
            return {"ok": False, "error": "没有可回滚的快照"}
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

    # ---- 主入口 ------------------------------------------------------
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
                "会话 SharedState 已损坏，已停止写入以保护原数据。"
                f"备份: {backup or '创建失败'}"
            )
        if consume_turn:
            _store.append_session_event(
                self.session_id,
                "user_turn_received",
                self._workflow_event_payload({"text": user_text}),
            )
            try:
                workflow = self._workflow.consume_turn(user_text, self._state)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Workflow consume_turn 失败")
                return self._error_reply(f"Workflow 状态错误: {exc}")
            if getattr(self.ctx, "adaptive", True):
                try:
                    self.profile.observe(user_text)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("profile.observe 失败,忽略: %s", exc)
        else:
            workflow = self._workflow.workflow
            if workflow is None:
                return self._error_reply("没有活动 Workflow")
        self._sync_workflow_intent_to_state()
        stage_text = user_text or workflow.intent.raw_text
        ctx = self._make_ctx()
        ctx.extra["user_text"] = stage_text
        ctx.extra["workflow_digest"] = self._workflow.digest()
        if workflow.execution_status == "completed" and workflow.outcome == "cancelled":
            try:
                from ..tools.state_tools import resolve_confirmation
                resolve_confirmation(ctx, user_text)
                self._state.save(_store.state_path(self.session_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug("取消 Workflow 时清理旧 Policy pending 失败: %s", exc)
            return self._workflow_cancelled_reply()
        current = self._workflow.current_stage()
        if current and current.execution_status == "waiting":
            try:
                self._state.save(_store.state_path(self.session_id))
            except OSError as exc:
                logger.warning("等待态 SharedState 落盘失败: %s", exc)
            return self._workflow_waiting_reply()
        stage = self._workflow.start_stage()
        if stage is None:
            return self._error_reply("Workflow 没有可执行 Stage")
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
            from ..tools.state_tools import (
                commit_intent,
                detect_recipe_switch,
                is_confirmation_utterance,
                is_read_only_request,
                redundant_recipe_switch,
                resolve_confirmation,
            )

            resolution = resolve_confirmation(ctx, user_text) if user_text else {}
            if resolution.get("resolved") and not resolution.get("approved"):
                self._state.save(_store.state_path(self.session_id))
                self._workflow.finish("cancelled")
                reply = AgentReply(
                    text="已取消待执行的工程修改。",
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
            if user_text and is_read_only_request(user_text):
                ctx.extra["mutation_forbidden"] = True
            if user_text and workflow.intent.turn_relation == "new_task" \
                    and not is_confirmation_utterance(user_text):
                commit_intent(ctx, user_text)
            target_recipe = detect_recipe_switch(self._state, user_text)
            if (
                not target_recipe
                and not ctx.extra.get("mutation_forbidden")
            ):
                already = redundant_recipe_switch(self._state, user_text)
                if already:
                    try:
                        self._state.save(_store.state_path(self.session_id))
                    except OSError as exc:
                        logger.warning("SharedState 落盘失败: %s", exc)
                    reply = AgentReply(
                        text="当前已经是 {}，无需换配方。".format(already),
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
        agent = None
        if stage.id not in _HOST_CONTROLLED_STAGES:
            try:
                agent = _orch.build_agent(ctx, stage=stage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("组装 deepagents 失败,降级到确定性骨架: %s", exc)

        try:
            if agent is not None:
                reply = self._run_deep(agent, ctx, stage_text)
            else:
                reply = self._run_stage_deterministic(
                    ctx, stage_text, recipe, simulate, stage.id
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("编排执行异常")
            reply = self._error_reply(
                f"编排出错: {type(exc).__name__}: {exc}"
            )
        self._finish_workflow_reply(reply)
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("SharedState 落盘失败: %s", exc)
        return reply

    def _continue_autonomous(
        self, first: AgentReply, *, recipe: str, simulate: bool
    ) -> AgentReply:
        """Drive following autonomous Stages without reclassifying text."""
        reply = first
        for _ in range(MAX_AUTONOMOUS_STAGES_PER_TURN):
            workflow = self._workflow.workflow
            if workflow is None or workflow.execution_status in (
                "completed", "errored"
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
        ) + "自动 Stage 推进达到安全上限，已停止并保留当前状态。"
        return reply

    @staticmethod
    def _merge_reply_history(current: AgentReply, previous: AgentReply) -> None:
        artifacts = dict(previous.artifacts or {})
        artifacts.update(current.artifacts or {})
        current.artifacts = artifacts
        current.tool_invocations = list(previous.tool_invocations or []) + list(
            current.tool_invocations or []
        )

    def step_command(self, command: Dict[str, Any]) -> AgentReply:
        """Structured GUI command entry; text remains a compatibility transport."""
        action = str((command or {}).get("action") or "")
        if action != "checkpoint_decision":
            return self._error_reply(f"未知 GUI command: {action or '(empty)'}")
        stage = self._workflow.current_stage()
        checkpoint = stage.checkpoint if stage else None
        checkpoint_id = str(command.get("checkpoint_id") or "")
        if not checkpoint or checkpoint.id != checkpoint_id:
            return self._error_reply("Checkpoint 已变化，请刷新后重试。")
        decision = str(command.get("decision") or "")
        if decision not in ("approved", "rejected"):
            return self._error_reply("Checkpoint decision 必须是 approved/rejected。")
        _store.append_session_event(
            self.session_id,
            "checkpoint_command_received",
            self._workflow_event_payload(
                {"checkpoint_id": checkpoint_id, "decision": decision}
            ),
        )
        stage_id = stage.id if stage else ""
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
                        "当前受控发射进程不在有效运行窗口内，不能记录空口通过。"
                        "请先检查运行错误并重新执行有限时长发射。"
                    )
                    return reply
                if decision == "approved" and observed_name != expected_name:
                    reply = self._workflow_waiting_reply()
                    reply.stage = "CRITIC"
                    reply.text = (
                        f"空口观察名称与目标不一致：期望 {expected_name or '(空)'}，"
                        f"收到 {observed_name or '(未填写)'}。"
                    )
                    return reply
                artifact = _store.attach_evidence(
                    self.session_id, str(observation.get("artifact") or "")
                )
                ota_observation = {
                    "observed": decision == "approved",
                    "observed_name": observed_name,
                    "expected_name": expected_name,
                    "observed_at": now,
                    "run_id": status.get("run_id"),
                    "evidence_kind": str(
                        observation.get("evidence_kind") or "human_confirmation"
                    ),
                    "artifact": artifact,
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
        self._state.save(_store.state_path(self.session_id))
        if (
            self._workflow.workflow.execution_status == "completed"
            and self._workflow.workflow.outcome == "cancelled"
        ):
            return self._workflow_cancelled_reply()
        reply = self._step_once(
            "", recipe="", simulate=True, consume_turn=False
        )
        return self._continue_autonomous(reply, recipe="", simulate=True)

    # ---- 主路径:deepagents ------------------------------------------
    def _run_deep(self, agent: Any, ctx: ToolContext,
                  user_text: str) -> AgentReply:
        ctx.extra["execution_mode"] = "deepagents"
        config = {"configurable": {"thread_id": self.session_id},
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
                            "content": f"{user_text}\n\n当前 Stage TaskCard：{task_json}",
                        }
                    ]
                },
                config,
            )
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "GraphRecursionError":
                raise
            # 撞步数上限本身不代表任务失败:工具产物与 Claim 都已落在 ctx/state
            # 里,按已完成的部分如实交付,而不是把成功结果显示成 ERROR。
            logger.warning("编排达到步数上限,按已产出结果交付: %s", exc)
            _store.append_session_event(
                self.session_id, "recursion_limit",
                {"limit": config["recursion_limit"]})
            done = bool(ctx.extra.get("artifacts", {}).get("grc_path")) or bool(
                self._state.project.grc_path)
            note = ("编排步数达到上限,已中止后续探索。"
                    if done else "编排步数达到上限,尚未产出可用流图。")
            return self._fold(ctx, note, source="deepagents-truncated",
                              ok=done)

        narrative = self._extract_final_text(result)
        self._record_deep_delegations(result, ctx)
        # deepagents 把会话文件放在 state 的 "files" 键:镜像到磁盘
        files = result.get("files") if isinstance(result, dict) else None
        if files:
            _store.mirror_session_files(self.session_id, files)
        return self._fold(ctx, narrative, source="deepagents")

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
                        "subagent_invoked",
                        self._workflow_event_payload({
                            "target_agent": target,
                            "description": (args or {}).get("description"),
                            "task_id": invocation.get("task_id"),
                            "call_id": call_id,
                            "mode": "deepagents",
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

        # 环境级失败(缺 platform/gnuradio):如实报告,不伪装成建图结果
        if not result.get("ok") and "recipe" not in result:
            err = result.get("error", "建图环境不可用")
            _store.append_session_event(self.session_id, "env_error",
                                        {"error": err})
            return self._error_reply(f"建图环境不可用: {err}")

        # 把 design_link 结果并入 ctx,复用统一折叠
        _merge(ctx.extra.setdefault("artifacts", {}),
               result.get("artifacts", {}))
        if result.get("metrics"):
            ctx.extra.setdefault("metrics", {}).update(result["metrics"])
        ctx.extra["events"].append({"kind": "design_link", "payload": {
            "recipe": result.get("recipe"), "valid": result.get("valid"),
            "steps": result.get("steps", []),
            "policy": result.get("policy"),
            "error": result.get("error")}})
        narrative = result.get("narrative") or self._fallback_text(result)
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
        """Minimal deterministic handlers sharing the same Stage semantics as LLM."""
        from ..tools import registry

        active = self._state.coordination.active_task
        _store.append_session_event(
            self.session_id,
            "subagent_invoked",
            self._workflow_event_payload({
                "target_agent": active.target_agent if active else "stage_handler",
                "stage_id": stage_id,
                "mode": "deterministic",
            }),
        )

        capabilities = set(
            self._workflow.workflow.intent.capabilities
            if self._workflow.workflow else []
        )
        if (
            "hardware_configure" in capabilities
            and stage_id in {
                "build_and_verify", "tx_build_and_validate",
                "rx_build_and_verify", "apply_and_verify",
            }
        ):
            if self._hardware_rx_spectrum_ready():
                return self._run_hardware_rx_spectrum(ctx)
            return self._fold(
                ctx,
                "当前无可证明满足全部硬件能力的确定性模板；已停止，避免用离线仿真配方替代用户目标。",
                source="deterministic-stage",
                ok=False,
            )

        if stage_id in {
            "build_and_verify", "tx_build_and_validate", "rx_build_and_verify"
        }:
            return self._run_deterministic(ctx, user_text, recipe, simulate)
        if stage_id == "apply_and_verify":
            target_recipe = str(
                (
                    self._workflow.workflow.intent.slots
                    if self._workflow.workflow
                    else {}
                ).get("target_recipe")
                or ""
            )
            if target_recipe:
                return self._run_deterministic(
                    ctx, user_text, target_recipe, simulate
                )
            if recipe:
                return self._run_deterministic(ctx, user_text, recipe, simulate)
            request_text = (
                self._workflow.workflow.intent.raw_text
                if self._workflow.workflow else user_text
            )
            change = re.search(
                r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*(?:改为|设为|改成|=)\s*([^，。\s]+)",
                request_text,
            )
            if not change:
                return self._fold(
                    ctx, "无法从修改请求中确定 block.parameter 和新值。",
                    source="deterministic-stage", ok=False,
                )
            result = registry.call("apply_grc_diff", {
                "block_id": change.group(1),
                "parameter": change.group(2),
                "value": change.group(3),
                "resimulate": simulate,
            }, ctx)
            self._record_tool_result(ctx, "apply_grc_diff", result)
            validation = registry.call("validate_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "validate_flowgraph", validation)
            if result.get("path"):
                ctx.extra.setdefault("artifacts", {})["grc_path"] = result["path"]
            return self._fold(
                ctx,
                result.get("error") or (
                    f"已修改 {change.group(1)}.{change.group(2)}，完成重验。"
                ),
                source="deterministic-stage",
                ok=bool(result.get("ok")) and bool(validation.get("valid")),
            )
        if stage_id == "inspect_and_plan":
            result = registry.call("inspect_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "inspect_flowgraph", result)
            note = (
                "已检查当前工程并形成变更计划；确认后才会应用并重验。"
                if result.get("ok") else result.get("error", "工程检查失败")
            )
            return self._fold(
                ctx, note, source="deterministic-stage", ok=bool(result.get("ok"))
            )
        if stage_id in ("inspect_and_measure", "inspect_and_diagnose"):
            return self._inspect_measure_stage(
                ctx, diagnose=stage_id == "inspect_and_diagnose"
            )
        if stage_id == "hardware_precheck":
            hardware = str(
                (self._workflow.workflow.intent.slots if self._workflow.workflow else {}).get("hardware") or ""
            )
            result = registry.call(
                "hardware_preflight", {"device_type": hardware}, ctx
            )
            self._record_tool_result(ctx, "hardware_preflight", result)
            missing = list(result.get("missing") or [])
            note = result.get("note") or "硬件预检完成。"
            if missing:
                note = "硬件预检尚缺：{}。{}".format(", ".join(missing), note)
            return self._fold(
                ctx, note,
                source="deterministic-stage", ok=bool(result.get("ok")),
            )
        if stage_id == "configure_and_check":
            slots = self._workflow.workflow.intent.slots if self._workflow.workflow else {}
            result = registry.call("configure_sdr", {
                "device_type": slots.get("hardware") or "sdr",
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
            }, ctx)
            self._record_tool_result(ctx, "configure_sdr", result)
            preflight = registry.call(
                "hardware_preflight",
                {"device_type": slots.get("hardware") or "sdr"},
                ctx,
            )
            self._record_tool_result(ctx, "hardware_preflight", preflight)
            return self._fold(
                ctx,
                result.get("error") or "SDR 参数已记录；真实硬件操作保持禁用。",
                source="deterministic-stage",
                ok=bool(result.get("ok")) and bool(preflight.get("ok")),
            )
        if stage_id == "build_ble_advertiser":
            slots = self._workflow.workflow.intent.slots
            local_name = str(slots.get("local_name") or "")
            channel = int((slots.get("advertising_channels") or [37])[0])
            hardware = normalize_sdr_hardware(str(slots.get("hardware") or "b210"))
            profile = resolve_hardware_profile(hardware)
            pdu_args = {
                "local_name": local_name, "channel": channel,
            }
            pdu = registry.call("build_ble_advertising_pdu", pdu_args, ctx)
            self._record_tool_result(
                ctx, "build_ble_advertising_pdu", pdu, pdu_args
            )
            waveform_args = {
                "local_name": local_name,
                "channel": channel,
                "sample_rate": slots.get("sample_rate") or 2e6,
                "interval_ms": slots.get("advertising_interval_ms") or 100.0,
                "bt": slots.get("bt") or 0.5,
                "modulation_index": slots.get("modulation_index") or 0.5,
                "digital_amplitude": slots.get("digital_amplitude") or 0.5,
            }
            waveform = registry.call("generate_ble_1m_waveform", waveform_args, ctx)
            self._record_tool_result(
                ctx, "generate_ble_1m_waveform", waveform, waveform_args
            )
            if profile is None or not profile.ble_tx_builder:
                return self._fold(
                    ctx,
                    f"所选硬件 {hardware or '(empty)'} 暂无 BLE TX builder；"
                    "已停止，未替换成其他 SDR。",
                    source="deterministic-stage",
                    ok=False,
                )
            if profile.ble_tx_builder == "build_ble_pluto_tx_flowgraph":
                build_args = {
                    "waveform_path": waveform.get("path") or "",
                    "channel": channel,
                    "sample_rate": slots.get("sample_rate") or 2e6,
                    "attenuation": slots.get("tx_attenuation", 30.0),
                    "uri": slots.get("device_uri") or "",
                    "duration_seconds": slots.get("duration_seconds") or 30.0,
                }
                built = registry.call(
                    "build_ble_pluto_tx_flowgraph", build_args, ctx
                )
                builder = "build_ble_pluto_tx_flowgraph"
                sink_note = "PlutoSDR TX 流图已生成；尚未启动 RF。"
            elif profile.ble_tx_builder == "build_ble_uhd_tx_flowgraph":
                build_args = {
                    "waveform_path": waveform.get("path") or "",
                    "channel": channel,
                    "sample_rate": slots.get("sample_rate") or 2e6,
                    "gain": slots.get("tx_gain", 0.0),
                    "device_args": "type=b200",
                    "duration_seconds": slots.get("duration_seconds") or 30.0,
                }
                built = registry.call(
                    "build_ble_uhd_tx_flowgraph", build_args, ctx
                )
                builder = "build_ble_uhd_tx_flowgraph"
                sink_note = "B210 TX 流图已生成；尚未启动 RF。"
            else:
                return self._fold(
                    ctx,
                    f"HardwareProfile {profile.key} 的 BLE builder 未实现。",
                    source="deterministic-stage",
                    ok=False,
                )
            self._record_tool_result(ctx, builder, built, build_args)
            if built.get("grc_path"):
                ctx.extra.setdefault("artifacts", {})["grc_path"] = built["grc_path"]
                self._state.project.grc_path = built["grc_path"]
                self._state.project.flowgraph_version += 1
                self._state.project.config.update({
                    "protocol": "ble",
                    "local_name": local_name,
                    "ble_channel": channel,
                    "hardware": hardware,
                    "rf_armed": False,
                    "desired_device": {
                        "type": hardware,
                        "center_freq": slots.get("carrier_frequency"),
                        "sample_rate": slots.get("sample_rate"),
                    },
                })
            return self._fold(
                ctx,
                built.get("error") or f"BLE 广播 PDU、离线波形和{sink_note}",
                source="deterministic-stage",
                ok=bool(pdu.get("ok") and waveform.get("ok") and built.get("ok")),
            )
        if stage_id == "offline_protocol_verify":
            slots = self._workflow.workflow.intent.slots
            channel = int((slots.get("advertising_channels") or [37])[0])
            verify_args = {
                "local_name": slots.get("local_name") or "", "channel": channel,
            }
            verified = registry.call("verify_ble_packet_bits", verify_args, ctx)
            self._record_tool_result(
                ctx, "verify_ble_packet_bits", verified, verify_args
            )
            validation = registry.call("validate_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "validate_flowgraph", validation)
            hardware = normalize_sdr_hardware(str(slots.get("hardware") or "b210"))
            profile = resolve_hardware_profile(hardware)
            sink = profile.label if profile else hardware or "SDR"
            return self._fold(
                ctx, f"BLE PDU/CRC/whitening 与 {sink} TX 流图离线校验完成。",
                source="deterministic-stage",
                ok=bool(verified.get("valid") and validation.get("valid")),
            )
        if stage_id == "discover_and_probe_device":
            slots = self._workflow.workflow.intent.slots
            hardware = normalize_sdr_hardware(str(slots.get("hardware") or "b210"))
            profile = resolve_hardware_profile(hardware)
            if profile is None:
                return self._fold(
                    ctx, f"不支持的 SDR 类型: {hardware or '(empty)'}。",
                    source="deterministic-stage", ok=False,
                )
            args = {"device_type": hardware}
            if hardware == "b210":
                args["device_args"] = "type=b200"
            discovered = registry.call("discover_devices", args, ctx)
            self._record_tool_result(ctx, "discover_devices", discovered, args)
            if discovered.get("device_identity"):
                args["device_args"] = discovered["device_identity"]
            probed = registry.call("probe_device", args, ctx)
            self._record_tool_result(ctx, "probe_device", probed, args)
            if discovered.get("device_found") and probed.get("device_probed"):
                self._state.project.config["observed_device"] = {
                    "type": profile.key,
                    "identity": probed.get("device_identity")
                    or discovered.get("device_identity"),
                    "driver_family": profile.driver_family,
                }
            label = profile.label
            if not discovered.get("device_found"):
                note = discovered.get("error") or f"未发现可用 {label}。"
            elif not discovered.get("device_identity"):
                note = f"已发现 {label}，但未能提取可用于精确探测的设备标识。"
            elif not probed.get("device_probed"):
                note = (
                    probed.get("error")
                    or f"已发现 {label} {discovered.get('device_identity')}，"
                    "但精确 probe 未通过验收。"
                )
            else:
                note = f"{label} 只读发现与 probe 完成；尚未打开 TX stream。"
            return self._fold(
                ctx,
                note,
                source="deterministic-stage",
                ok=bool(discovered.get("device_found") and probed.get("device_probed")),
            )
        if stage_id == "discover_and_probe_hardware":
            slots = self._workflow.workflow.intent.slots
            hardware = normalize_sdr_hardware(str(slots.get("hardware") or ""))
            profile = resolve_hardware_profile(hardware)
            if profile is None:
                return self._fold(
                    ctx, f"不支持的 SDR 类型: {hardware or '(empty)'}。",
                    source="deterministic-stage", ok=False,
                )
            args = {"device_type": hardware}
            discovered = registry.call(
                "discover_devices", args, ctx
            )
            self._record_tool_result(ctx, "discover_devices", discovered, args)
            if discovered.get("device_identity"):
                args["device_args"] = discovered["device_identity"]
            probed = registry.call(
                "probe_device", args, ctx
            )
            self._record_tool_result(ctx, "probe_device", probed, args)
            if not discovered.get("device_found"):
                note = discovered.get("error") or f"未发现可用 {profile.label}。"
            elif not discovered.get("device_identity"):
                note = (
                    f"已发现 {profile.label}，但未能提取可用于精确探测的设备标识。"
                )
            elif not probed.get("device_probed"):
                note = (
                    probed.get("error")
                    or f"已发现 {profile.label} {discovered.get('device_identity')}，"
                    "但精确 probe 未通过验收。"
                )
            else:
                note = f"{profile.label} 只读发现与探测完成；尚未启动 Flowgraph。"
            return self._fold(
                ctx,
                note,
                source="deterministic-stage",
                ok=bool(discovered.get("device_found") and probed.get("device_probed")),
            )
        if stage_id == "configure_device":
            slots = self._workflow.workflow.intent.slots
            configure_args = {
                "device_type": slots.get("hardware") or "b210",
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
            }
            result = registry.call("configure_sdr", configure_args, ctx)
            self._record_tool_result(ctx, "configure_sdr", result, configure_args)
            hardware = normalize_sdr_hardware(str(slots.get("hardware") or "b210"))
            profile = resolve_hardware_profile(hardware)
            label = profile.label if profile else hardware or "SDR"
            armed = {"ok": True, "armed": False}
            if str(slots.get("protocol") or "").lower() == "ble" and result.get("ok"):
                arm_args = {
                    "grc_path": self._state.project.grc_path,
                    "device_identity": str(
                        (self._state.project.config.get("observed_device") or {}).get(
                            "identity"
                        )
                        or ""
                    ),
                }
                armed = registry.call("arm_hardware_flowgraph", arm_args, ctx)
                self._record_tool_result(
                    ctx, "arm_hardware_flowgraph", armed, arm_args
                )
            return self._fold(
                ctx,
                result.get("error") or armed.get("error")
                or f"{label} 发射配置已记录并完成受控武装，等待启动。",
                source="deterministic-stage",
                ok=bool(result.get("ok") and armed.get("ok")),
            )
        if stage_id == "transmit_bounded":
            slots = self._workflow.workflow.intent.slots
            start_args = {
                "grc_path": self._state.project.grc_path,
                "duration_seconds": slots.get("duration_seconds") or 30.0,
            }
            result = registry.call("start_flowgraph", start_args, ctx)
            self._record_tool_result(ctx, "start_flowgraph", result, start_args)
            return self._fold(
                ctx,
                result.get("error")
                or (
                    f"BLE 受控发射已就绪，run_id={result.get('run_id')}；"
                    f"请在截止时间前检查广播名称 {slots.get('local_name') or '(未指定)'}。"
                ),
                source="deterministic-stage",
                ok=bool(result.get("running") and result.get("ready")),
            )
        if stage_id == "run_bounded":
            slots = self._workflow.workflow.intent.slots
            result = registry.call("start_flowgraph", {
                "grc_path": self._state.project.grc_path,
                "duration_seconds": slots.get("duration_seconds") or 30.0,
            }, ctx)
            self._record_tool_result(ctx, "start_flowgraph", result)
            return self._fold(
                ctx,
                result.get("error") or "硬件 Flowgraph 已按有界时长启动，请检查实时界面。",
                source="deterministic-stage",
                ok=bool(result.get("running") and result.get("ready")),
            )
        if stage_id == "stop_and_finalize":
            stopped = registry.call("stop_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "stop_flowgraph", stopped)
            observed = bool(self._workflow.workflow.intent.slots.get("over_air_observed"))
            ota = dict(
                self._workflow.workflow.intent.slots.get("ota_observation") or {}
            )
            same_run = bool(
                ota.get("run_id")
                and ota.get("run_id") == stopped.get("run_id")
            )
            return self._fold(
                ctx,
                "发射已停止，LightBlue 空口观察已记录。"
                if observed else "发射已停止，但用户未在 LightBlue 中观察到目标广播。",
                source="deterministic-stage",
                ok=bool(
                    stopped.get("ok")
                    and not stopped.get("crashed")
                    and stopped.get("run_id")
                    and observed
                    and same_run
                ),
            )
        if stage_id == "stop_runtime":
            stopped = registry.call("stop_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "stop_flowgraph", stopped)
            return self._fold(
                ctx,
                "硬件 Flowgraph 已停止，运行状态与用户观察结果已记录。",
                source="deterministic-stage",
                ok=bool(
                    stopped.get("ok")
                    and not stopped.get("running")
                    and not stopped.get("crashed")
                    and stopped.get("run_id")
                ),
            )
        if stage_id == "repair_and_verify":
            diagnosed = self._workflow.workflow.stage("inspect_and_diagnose")
            changes = list((diagnosed.result if diagnosed else {}).get("proposed_changes") or [])
            if not changes:
                return self._fold(
                    ctx, "没有可确定执行的修复参数，请先补充修改目标。",
                    source="deterministic-stage", ok=False,
                )
            change = changes[0]
            result = registry.call("apply_grc_diff", {
                "block_id": change.get("block_id"),
                "parameter": change.get("parameter"),
                "value": change.get("value"),
                "resimulate": simulate,
            }, ctx)
            self._record_tool_result(ctx, "apply_grc_diff", result)
            validation = registry.call("validate_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "validate_flowgraph", validation)
            if result.get("path"):
                ctx.extra.setdefault("artifacts", {})["grc_path"] = result["path"]
            return self._fold(
                ctx, result.get("error") or "已应用最小修复并完成重验。",
                source="deterministic-stage",
                ok=bool(result.get("ok")) and bool(validation.get("valid")),
            )
        return self._fold(
            ctx, f"Stage {stage_id} 没有可安全自动执行的确定性修改。",
            source="deterministic-stage", ok=False,
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
            "build_usrp_rx_spectrum_flowgraph",
            {
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
                "device_args": "type=b200",
            },
            ctx,
        )
        self._record_tool_result(ctx, "build_usrp_rx_spectrum_flowgraph", result)
        if result.get("grc_path"):
            ctx.extra.setdefault("artifacts", {})["grc_path"] = result["grc_path"]
        validation = registry.call("validate_flowgraph", {}, ctx)
        self._record_tool_result(ctx, "validate_flowgraph", validation)
        note = result.get("error") or (
            "已生成 B210 接收实时频谱流图（QT GUI Frequency Sink）。"
            "尚未启动 RF；实时频谱会在 GNU Radio QT 窗口中显示，而不是对话里的 PNG。"
            if result.get("ok")
            else "无法生成 B210 接收频谱流图。"
        )
        return self._fold(
            ctx, note, source="deterministic-stage",
            ok=bool(result.get("ok")) and bool(validation.get("valid")),
        )

    def _inspect_measure_stage(
        self, ctx: ToolContext, *, diagnose: bool = False
    ) -> AgentReply:
        from ..tools import registry

        inspected = registry.call("inspect_flowgraph", {}, ctx)
        self._record_tool_result(ctx, "inspect_flowgraph", inspected)
        validation = registry.call("validate_flowgraph", {}, ctx)
        self._record_tool_result(ctx, "validate", validation)
        if not inspected.get("ok") or not validation.get("ok"):
            return self._fold(
                ctx, inspected.get("error") or validation.get("error") or "工程检查失败",
                source="deterministic-stage", ok=False,
            )
        if not validation.get("valid"):
            explained = registry.call(
                "explain_error", {"errors": validation.get("errors") or []}, ctx
            )
            self._record_tool_result(ctx, "explain_error", explained)
            return self._fold(
                ctx, "结构校验未通过，已给出具体错误与修复建议。",
                source="deterministic-stage", ok=False,
            )
        simulated = registry.call("run_simulation", {}, ctx)
        self._record_tool_result(ctx, "simulate", simulated)
        if not simulated.get("ok"):
            return self._fold(
                ctx, simulated.get("error") or "仿真失败",
                source="deterministic-stage", ok=False,
            )
        workflow = self._workflow.workflow
        slots = workflow.intent.slots if workflow else {}
        requested = list(slots.get("requested_metrics") or [])
        if diagnose and not requested:
            requested = ["evm"]
        if not requested:
            requested = ["spectrum"]
        modulation = str(
            self._state.project.config.get("modulation") or slots.get("modulation") or "bpsk"
        )
        sps = 1 if str(self._state.project.config.get("recipe") or "").startswith("rx_") else 4
        metrics = ctx.extra.setdefault("metrics", {})
        for kind in requested:
            if kind in ("evm", "ber", "spectrum"):
                args = {
                    "kind": kind,
                    "modulation": modulation,
                    "sps": sps,
                }
                if kind == "ber":
                    args.update({"probe_id": "sink", "tx_bits_probe": "tx_sink"})
                measured = registry.call("read_metric", args, ctx)
                self._record_tool_result(ctx, "read_metric", measured)
                if measured.get("ok") and measured.get("value") is not None:
                    metrics["evm_pct" if kind == "evm" else (
                        "spectrum_peak" if kind == "spectrum" else "ber"
                    )] = measured["value"]
                    if measured.get("peak_bin") is not None:
                        metrics["spectrum_peak_bin"] = measured["peak_bin"]
            plot_name = {
                "spectrum": "plot_spectrum",
                "constellation": "plot_constellation",
                "eye": "plot_eye",
            }.get(kind)
            if plot_name:
                plotted = registry.call(plot_name, {"sps": sps} if plot_name != "plot_spectrum" else {}, ctx)
                self._record_tool_result(ctx, plot_name, plotted)
                if plotted.get("path"):
                    key = {
                        "plot_spectrum": "spectrum_png",
                        "plot_constellation": "constellation_png",
                        "plot_eye": "eye_png",
                    }[plot_name]
                    ctx.extra.setdefault("artifacts", {})[key] = plotted["path"]
        if diagnose:
            diagnosis = registry.call("debug_by_metric", {
                "metric": "evm" if "evm" in requested else "spectrum",
                "modulation": modulation,
                "sps": sps,
            }, ctx)
            self._record_tool_result(ctx, "debug_by_metric", diagnosis)
            issue = "偏高" in str(diagnosis.get("verdict") or "")
            reply = self._fold(
                ctx,
                diagnosis.get("narrative") or diagnosis.get("error") or "诊断完成。",
                source="deterministic-stage", ok=not issue and bool(diagnosis.get("ok")),
            )
            if issue:
                block = ctx.blocks.get("chan")
                parameter = (getattr(block, "params", None) or {}).get("noise_voltage")
                try:
                    value = max(float(parameter.get_value()) / 2.0, 0.0)
                except (AttributeError, TypeError, ValueError):
                    value = 0.02
                reply.pending = {
                    "action": "workflow_checkpoint",
                    "reason": "EVM 偏高，应用最小噪声参数修复",
                    "approved": False,
                    "proposed_changes": [{
                        "block_id": "chan",
                        "parameter": "noise_voltage",
                        "value": value,
                    }],
                }
            return reply
        return self._fold(
            ctx, "工程检查与测量完成。", source="deterministic-stage", ok=True
        )

    @staticmethod
    def _record_tool_result(
        ctx: ToolContext,
        kind: str,
        result: Dict[str, Any],
        args: Optional[Dict[str, Any]] = None,
    ) -> None:
        ctx.extra.setdefault("events", []).append(
            {
                "kind": kind,
                "args": dict(args or {}),
                "payload": dict(result or {}),
            }
        )

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
                str(pending.get("reason") or pending.get("action") or "等待用户确认"),
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
            if not envelope.ok and reply.stage not in ("ERROR", "DENY", "CONFIRM"):
                reply.stage = "CRITIC"
                reply.needs_confirmation = False
                if missing_completion:
                    reply.text = "{}\n尚未满足 Stage 完成条件：{}。".format(
                        reply.text, ", ".join(missing_completion)
                    ).strip()
                failure_codes = list(envelope.acceptance.get("failure_codes") or [])
                if failure_codes:
                    reply.text = "{}\nStage 验收失败：{}。".format(
                        reply.text, ", ".join(failure_codes)
                    ).strip()
            result = vars(envelope)
            result["errored"] = reply.stage == "ERROR"
            result["improvement_available"] = any(
                invocation.name in ("explain_error", "debug_by_metric", "diagnose_by_metric")
                for invocation in reply.tool_invocations
            )
            if ok is not None and not bool(ok):
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
            pending_items = list(self._state.coordination.pending_confirmations or [])
            unresolved = next(
                (item for item in reversed(pending_items) if not item.get("approved")),
                None,
            )
            if unresolved is not None:
                unresolved.setdefault("id", f"pending-{uuid.uuid4().hex[:8]}")
                unresolved["checkpoint_id"] = current.checkpoint.id
            reply.stage = "CONFIRM"
            reply.needs_confirmation = True
            if unresolved is not None:
                reply.pending = dict(unresolved)
            elif not reply.pending:
                reply.pending = {
                    "action": "workflow_checkpoint",
                    "reason": current.checkpoint.reason,
                    "checkpoint_id": current.checkpoint.id,
                    "approved": False,
                }
        reply.workflow_digest = self._digest_with_timeline()
        reply.done = reply.workflow_digest.get("execution_status") == "completed"
        reply.claims = ClaimStore(self._state).summary()
        reply.spec_digest = self._state.spec_digest()

    def _project_stage_effects(self, stage: Any, reply: AgentReply) -> None:
        """Commit host-observed artifacts for deterministic and LLM executors."""
        grc_path = str((reply.artifacts or {}).get("grc_path") or "")
        if not grc_path:
            return
        successful_tools = {
            item.name for item in (reply.tool_invocations or []) if item.ok
        }
        mutating_tools = {
            "design_link",
            "design_flowgraph",
            "render_grc",
            "apply_grc_diff",
            "apply_flowgraph_patch",
            "build_ble_uhd_tx_flowgraph",
            "build_ble_pluto_tx_flowgraph",
            "arm_hardware_flowgraph",
            "build_usrp_rx_spectrum_flowgraph",
        }
        if not successful_tools.intersection(mutating_tools):
            return
        workflow = self._workflow.workflow
        self._state.project.grc_path = grc_path
        semantic_hash = _flowgraph_semantic_hash(grc_path)
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
            ):
                value = slots.get(key)
                if value not in (None, "", []):
                    self._state.project.config[key] = value
            hardware = str(slots.get("hardware") or "")
            if hardware:
                self._state.project.config["desired_device"] = {
                    "type": hardware,
                    "center_freq": slots.get("carrier_frequency"),
                    "sample_rate": slots.get("sample_rate"),
                }

    def _project_tool_results(self, stage: Any, reply: AgentReply) -> None:
        """Project host-observed tool facts identically for every executor."""
        results: Dict[str, list[Dict[str, Any]]] = {}
        for invocation in reply.tool_invocations or []:
            if isinstance(invocation.result, dict):
                results.setdefault(invocation.name, []).append(invocation.result)
        discovered = next(
            (item for item in reversed(results.get("discover_devices", []))
             if item.get("device_found")),
            None,
        )
        probed = next(
            (item for item in reversed(results.get("probe_device", []))
             if item.get("device_probed")),
            None,
        )
        if discovered and probed:
            self._state.project.config["observed_device"] = {
                "type": probed.get("device_type") or discovered.get("device_type"),
                "identity": probed.get("device_identity")
                or discovered.get("device_identity"),
                "driver_family": probed.get("driver_family")
                or discovered.get("driver_family"),
            }
            self._record_claim(
                "hardware_device_probed",
                "Selected SDR was discovered and probed by its explicit identity",
                "hardware",
                "discover_and_probe",
                self._state.project.config["observed_device"],
                True,
            )
        verified = next(
            (item for item in reversed(results.get("verify_ble_packet_bits", []))
             if item.get("valid")),
            None,
        )
        if verified:
            self._record_claim(
                "ble_offline_protocol_valid",
                "BLE packet and IQ waveform passed independent offline validation",
                "structure",
                "verify_ble_packet_bits",
                {"checks": dict(verified.get("checks") or {})},
                True,
            )
        armed = next(
            (item for item in reversed(results.get("arm_hardware_flowgraph", []))
             if item.get("ok") and item.get("armed")),
            None,
        )
        if armed:
            self._state.project.config["rf_armed"] = True
            self._state.project.config["rf_armed_path"] = armed.get("grc_path")
            semantic_hash = _flowgraph_semantic_hash(str(armed.get("grc_path") or ""))
            if semantic_hash:
                self._state.project.config["flowgraph_semantic_hash"] = semantic_hash
        started = next(
            (item for item in reversed(results.get("start_flowgraph", []))
             if item.get("ok") and item.get("running") and item.get("ready")
             and item.get("startup_health_passed") and item.get("run_id")),
            None,
        )
        if started:
            self._record_claim(
                "rf_runtime_started",
                "Bounded RF runtime was started by the controlled service",
                "hardware",
                "start_flowgraph",
                {
                    "pid": started.get("pid"),
                    "run_id": started.get("run_id"),
                    "duration_seconds": started.get("duration_seconds"),
                    "program": started.get("program"),
                },
                True,
            )
        terminal = next(
            (
                item
                for name in (
                    "stop_flowgraph", "emergency_stop", "query_runtime_status"
                )
                for item in reversed(results.get(name, []))
                if item.get("run_id") and not item.get("running")
            ),
            None,
        )
        if terminal:
            clean = bool(
                terminal.get("ok")
                and not terminal.get("crashed")
                and terminal.get("reason")
                in {"stopped", "emergency_stop", "exited"}
                and terminal.get("return_code") in (0, -15, -9)
            )
            self._record_claim(
                "rf_runtime_completed_cleanly",
                "Controlled RF runtime reached a verified terminal state",
                "hardware",
                "runtime_terminal_status",
                {
                    "run_id": terminal.get("run_id"),
                    "reason": terminal.get("reason"),
                    "return_code": terminal.get("return_code"),
                    "crashed": bool(terminal.get("crashed")),
                },
                clean,
                artifact=str(terminal.get("log_path") or ""),
            )
            if not clean and ClaimStore(self._state).get("rf_runtime_started"):
                self._record_claim(
                    "rf_runtime_started",
                    "Bounded RF runtime was started by the controlled service",
                    "hardware",
                    "runtime_failure",
                    {
                        "run_id": terminal.get("run_id"),
                        "return_code": terminal.get("return_code"),
                        "reason": terminal.get("reason"),
                    },
                    False,
                    artifact=str(terminal.get("log_path") or ""),
                )

    def _record_claim(
        self,
        claim_id: str,
        statement: str,
        layer: str,
        test: str,
        observation: Dict[str, Any],
        passed: bool,
        artifact: str = "",
    ) -> None:
        store = ClaimStore(self._state)
        version = int(self._state.project.flowgraph_version)
        store.upsert(Claim(
            id=claim_id,
            statement=statement,
            layer=layer,
            status="NotTested",
            project_version=version,
        ))
        store.add_evidence(
            claim_id,
            Evidence(
                test=test,
                observation=dict(observation or {}),
                project_version=version,
                artifact=artifact,
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
        self._record_claim(
            "ota_ble_local_name_observed",
            "External receiver observed the requested BLE Complete Local Name",
            "hardware",
            source,
            details,
            observed,
            artifact=str(details.get("artifact") or ""),
        )

    def _digest_with_timeline(self) -> Dict[str, Any]:
        digest = self._workflow.digest()
        digest["timeline"] = _store.recent_events(self.session_id, limit=40)
        runtime = dict(self._state.project.config.get("runtime") or {})
        if runtime:
            deadline = float(runtime.get("deadline") or 0)
            runtime["remaining_seconds"] = max(0.0, deadline - time.time()) \
                if runtime.get("running") and deadline else 0.0
            digest["runtime"] = runtime
        return digest

    def _workflow_waiting_reply(self) -> AgentReply:
        stage = self._workflow.current_stage()
        intent = self._workflow.workflow.intent if self._workflow.workflow else None
        missing = list(getattr(intent, "missing_slots", None) or [])
        validation_errors = list(
            getattr(intent, "validation_errors", None) or []
        )
        if missing:
            labels = {
                "modulation": "请说明调制方式（如 BPSK、QPSK 或 OFDM）。",
                "current_project": "当前没有可供检查的工程，请先构建或打开一个 .grc。",
                "hardware": "请说明 SDR 设备类型（如 USRP B210）。",
                "carrier_frequency": "请说明中心频率（如 2.4 GHz）。",
                "sample_rate": "请说明采样率（如 1 MHz）。",
                "local_name": "请说明要广播的 BLE Complete Local Name。",
            }
            text = "\n".join(labels.get(item, f"请补充 {item}。") for item in missing)
            pending = {}
        elif validation_errors:
            labels = {
                "carrier_frequency_invalid": "中心频率格式或数值无效，请重新说明。",
                "carrier_frequency_out_of_device_range": "中心频率超出所选设备能力范围，请重新说明。",
                "sample_rate_invalid": "采样率必须为正数，请重新说明。",
                "bandwidth_invalid": "带宽必须为正数，请重新说明。",
                "symbol_rate_invalid": "符号率必须为正数，请重新说明。",
            }
            text = "\n".join(
                labels.get(item, f"参数校验失败：{item}。")
                for item in validation_errors
            )
            pending = {}
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
                "reason": stage.checkpoint.reason if stage and stage.checkpoint else "",
                "checkpoint_id": stage.checkpoint.id if stage and stage.checkpoint else "",
                "stage_id": stage.id,
                "approved": False,
            }
            text = (
                "请仅在 LightBlue 中实际看到目标 Complete Local Name 后点击“已看到目标名称”。"
                if stage.id == "over_air_verification"
                else "当前 Stage 等待你的确认；确认后继续，取消则保留现有工程。"
            )
        else:
            pending = {}
            text = (
                "当前 Stage 未满足完成条件。请补充信息、调整方案，或明确要求重试；"
                "这不是批准型 Checkpoint。"
            )
        return AgentReply(
            text=text,
            stage="CONFIRM" if stage and stage.checkpoint else "CRITIC",
            needs_confirmation=bool(stage and stage.checkpoint),
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            pending=pending,
            workflow_digest=self._digest_with_timeline(),
        )

    def _workflow_cancelled_reply(self) -> AgentReply:
        return AgentReply(
            text="已取消当前 Workflow，现有工程保持不变。",
            stage="CANCELLED",
            done=True,
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            workflow_digest=self._digest_with_timeline(),
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
                ok=bool(payload.get("ok", True) if isinstance(payload, dict)
                        else True)))
            _store.append_session_event(
                self.session_id,
                "tool_called",
                self._workflow_event_payload(
                    {"tool": kind, "args": args, "result": payload}
                ),
            )

        # 从会话 final/ 补扫 .grc(deepagents 路径产物可能只在磁盘)
        final = _store.scan_final_artifacts(self.session_id)
        for name, path in final.items():
            if name.endswith(".grc"):
                artifacts.setdefault("grc_path", path)
            artifacts.setdefault(name, path)

        if ctx.extra.get("metrics"):
            artifacts["metrics"] = ctx.extra["metrics"]

        reply.artifacts = artifacts
        manifest = _store.write_artifact_manifest(self.session_id, artifacts)
        artifacts["manifest"] = manifest
        self._state.project.config["artifact_refs"] = {
            key: os.path.relpath(value, _store.session_root(self.session_id))
            for key, value in artifacts.items()
            if isinstance(value, str) and os.path.isfile(value)
        }
        export_dir = str(ctx.extra.get("export_dir") or "")
        if export_dir:
            for value in artifacts.values():
                if isinstance(value, str) and os.path.isfile(value):
                    _store.export_artifact(value, export_dir)
            _store.rewrite_exported_grc_paths(self.session_id, export_dir)
            _store.write_export_manifest(self.session_id, export_dir)
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
            or "当前工程"
        )
        to_recipe = result.get("recipe") or pending.get("recipe") or ""
        text = (
            f"将把当前工程从「{from_recipe}」换成「{to_recipe}」。"
            "确认后才会重建流图；现有 Claim 将在新版本上失效并重验。"
            "画布保持不变，直到你确认。"
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
        """Keep host Tool observations authoritative over model narration."""
        events = list(ctx.extra.get("events") or [])
        if not source.startswith("deepagents") or not events:
            return narrative or ServiceAgent._fallback_text(
                {"recipe": None, "metrics": ctx.extra.get("metrics")}
            )
        passed = []
        failed = []
        not_started = False
        for event in events:
            name = str(event.get("kind") or "")
            payload = event.get("payload") or {}
            if not name:
                continue
            if isinstance(payload, dict) and payload.get("ok") is False:
                failed.append(name)
            else:
                passed.append(name)
            if isinstance(payload, dict) and payload.get("not_started"):
                not_started = True
        parts = []
        if passed:
            parts.append("已完成工具：" + "、".join(dict.fromkeys(passed)) + "。")
        if failed:
            parts.append("未通过工具：" + "、".join(dict.fromkeys(failed)) + "。")
        artifacts = [
            os.path.basename(value)
            for value in (ctx.extra.get("artifacts") or {}).values()
            if isinstance(value, str) and value
        ]
        if artifacts:
            parts.append("已记录产物：" + "、".join(dict.fromkeys(artifacts)) + "。")
        if not_started:
            parts.append("流图尚未启动，不能据此声称 RF 或空口验收成功。")
        return "".join(parts) or "当前 Stage 已结束，详细事实见工具结果。"

    @staticmethod
    def _fallback_text(data: dict) -> str:
        recipe = data.get("recipe") or "所选配方"
        metrics = data.get("metrics") or {}
        parts = [f"已按「{recipe}」建图并校验。"]
        if metrics.get("evm_pct") is not None:
            parts.append(f"仿真 EVM ≈ {metrics['evm_pct']:.2f}%。")
        return "".join(parts)


def _merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in (src or {}).items():
        if v:
            dst[k] = v


def build_service_agent(session_id: Optional[str] = None,
                        profile: Any = None) -> ServiceAgent:
    """便捷构造入口(与参考实现 build_service_agent 命名对齐)。"""
    return ServiceAgent(session_id=session_id, profile=profile)
