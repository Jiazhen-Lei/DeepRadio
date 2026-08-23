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
import logging
import os
import re
import uuid
from typing import Any, Dict, Optional

from ..schema import AgentReply, ToolInvocation
from ..memory.profile import UserProfile
from ..state import ClaimStore, SharedState
from ..tools.registry import ToolContext
from ..workflow import WorkflowEngine
from . import orchestrator as _orch
from . import session_store as _store
from . import stage_executor as _stage_executor

logger = logging.getLogger(__name__)

#: 一轮编排允许的 LangGraph 超步数上限(可用 GRC_AGENT_RECURSION_LIMIT 覆盖)。
#: 6 个 subagent 的闭环路由 + 工具调用远超 50 步,过小会在正常流程中途撞限。
DEFAULT_RECURSION_LIMIT = 150


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

        输出目录优先用 GUI 通过 ``ctx.tool_ctx.out_dir`` 指定的目录;未指定则
        落到会话 ``final/``。
        """
        out_dir = (self.ctx.tool_ctx.out_dir or "").strip() \
            or os.path.join(_store.session_root(self.session_id), "final")
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
        if ctx.flow_graph is None:
            self._load_session_flowgraph(ctx)
        return ctx

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
        self._state.project.flowgraph_version += 1
        self._state.project.config["canvas_dirty"] = True
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
        if not stopped.get("already_stopped"):
            _store.append_session_event(
                self.session_id, "hardware_emergency_stop", stopped
            )
        return _store.archive_workflow(self.session_id)

    # ---- 主入口 ------------------------------------------------------
    def step(self, user_text: str, recipe: str = "",
             simulate: bool = True) -> AgentReply:
        """Consume one user turn through WorkflowEngine, then execute its Stage."""
        if getattr(self._state, "_load_failed", False):
            backup = getattr(self._state, "_corrupt_backup", "")
            return self._error_reply(
                "会话 SharedState 已损坏，已停止写入以保护原数据。"
                f"备份: {backup or '创建失败'}"
            )
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
        ctx = self._make_ctx()
        ctx.extra["user_text"] = user_text
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
            return self._workflow_waiting_reply()
        stage = self._workflow.start_stage()
        if stage is None:
            return self._error_reply("Workflow 没有可执行 Stage")
        if stage.execution_status == "waiting":
            return self._workflow_waiting_reply()
        ctx.extra["workflow"] = workflow.to_dict()
        ctx.extra["stage_id"] = stage.id
        task_card = _stage_executor.make_task_card(
            workflow, stage, self._state, user_text
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

            resolution = resolve_confirmation(ctx, user_text)
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
            if is_read_only_request(user_text):
                ctx.extra["mutation_forbidden"] = True
            if not is_confirmation_utterance(user_text):
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
        try:
            agent = _orch.build_agent(ctx, stage=stage)
        except Exception as exc:  # noqa: BLE001
            logger.warning("组装 deepagents 失败,降级到确定性骨架: %s", exc)
            agent = None

        try:
            if agent is not None:
                reply = self._run_deep(agent, ctx, user_text)
            else:
                reply = self._run_stage_deterministic(
                    ctx, user_text, recipe, simulate, stage.id
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
        return self.step("确认" if decision == "approved" else "取消修改")

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
            pdu = registry.call("build_ble_advertising_pdu", {
                "local_name": local_name, "channel": channel,
            }, ctx)
            self._record_tool_result(ctx, "build_ble_advertising_pdu", pdu)
            waveform = registry.call("generate_ble_1m_waveform", {
                "local_name": local_name,
                "channel": channel,
                "sample_rate": slots.get("sample_rate") or 2e6,
                "interval_ms": slots.get("advertising_interval_ms") or 100.0,
            }, ctx)
            self._record_tool_result(ctx, "generate_ble_1m_waveform", waveform)
            built = registry.call("build_ble_uhd_tx_flowgraph", {
                "waveform_path": waveform.get("path") or "",
                "channel": channel,
                "sample_rate": slots.get("sample_rate") or 2e6,
                "gain": slots.get("tx_gain", 0.0),
                "device_args": "type=b200",
            }, ctx)
            self._record_tool_result(ctx, "build_ble_uhd_tx_flowgraph", built)
            if built.get("grc_path"):
                ctx.extra.setdefault("artifacts", {})["grc_path"] = built["grc_path"]
                self._state.project.grc_path = built["grc_path"]
                self._state.project.flowgraph_version += 1
                self._state.project.config.update({
                    "protocol": "ble",
                    "local_name": local_name,
                    "ble_channel": channel,
                })
            return self._fold(
                ctx,
                built.get("error") or "BLE 广播 PDU、离线波形和 B210 TX 流图已生成；尚未启动 RF。",
                source="deterministic-stage",
                ok=bool(pdu.get("ok") and waveform.get("ok") and built.get("ok")),
            )
        if stage_id == "offline_protocol_verify":
            slots = self._workflow.workflow.intent.slots
            channel = int((slots.get("advertising_channels") or [37])[0])
            verified = registry.call("verify_ble_packet_bits", {
                "local_name": slots.get("local_name") or "", "channel": channel,
            }, ctx)
            self._record_tool_result(ctx, "verify_ble_packet_bits", verified)
            validation = registry.call("validate_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "validate_flowgraph", validation)
            return self._fold(
                ctx, "BLE PDU/CRC/whitening 与 UHD TX 流图离线校验完成。",
                source="deterministic-stage",
                ok=bool(verified.get("valid") and validation.get("valid")),
            )
        if stage_id == "discover_and_probe_device":
            discovered = registry.call("discover_devices", {"device_args": "type=b200"}, ctx)
            self._record_tool_result(ctx, "discover_devices", discovered)
            probed = registry.call("probe_device", {"device_args": "type=b200"}, ctx)
            self._record_tool_result(ctx, "probe_device", probed)
            return self._fold(
                ctx,
                "B210 只读发现与 probe 完成；尚未打开 TX stream。"
                if discovered.get("device_found") and probed.get("device_probed")
                else discovered.get("error") or probed.get("error") or "未发现可用 B210。",
                source="deterministic-stage",
                ok=bool(discovered.get("device_found") and probed.get("device_probed")),
            )
        if stage_id == "discover_and_probe_hardware":
            slots = self._workflow.workflow.intent.slots
            hardware = str(slots.get("hardware") or "")
            discovered = registry.call(
                "discover_devices", {"device_type": hardware}, ctx
            )
            self._record_tool_result(ctx, "discover_devices", discovered)
            probed = registry.call(
                "probe_device", {"device_type": hardware}, ctx
            )
            self._record_tool_result(ctx, "probe_device", probed)
            return self._fold(
                ctx,
                f"{hardware or 'SDR'} 只读发现与探测完成；尚未启动 Flowgraph。"
                if discovered.get("device_found") and probed.get("device_probed")
                else discovered.get("error") or probed.get("error") or "未发现所选 SDR。",
                source="deterministic-stage",
                ok=bool(discovered.get("device_found") and probed.get("device_probed")),
            )
        if stage_id == "configure_device":
            slots = self._workflow.workflow.intent.slots
            result = registry.call("configure_sdr", {
                "device_type": slots.get("hardware") or "b210",
                "center_freq": slots.get("carrier_frequency"),
                "sample_rate": slots.get("sample_rate"),
            }, ctx)
            self._record_tool_result(ctx, "configure_sdr", result)
            return self._fold(
                ctx, result.get("error") or "B210 发射配置已记录，等待受控启动。",
                source="deterministic-stage", ok=bool(result.get("ok")),
            )
        if stage_id == "transmit_bounded":
            slots = self._workflow.workflow.intent.slots
            result = registry.call("start_flowgraph", {
                "grc_path": self._state.project.grc_path,
                "duration_seconds": slots.get("duration_seconds") or 30.0,
            }, ctx)
            self._record_tool_result(ctx, "start_flowgraph", result)
            return self._fold(
                ctx,
                result.get("error") or "BLE 发射已按有界时长启动；请在 LightBlue 中检查 deepradio。",
                source="deterministic-stage", ok=bool(result.get("running")),
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
                source="deterministic-stage", ok=bool(result.get("running")),
            )
        if stage_id == "stop_and_finalize":
            stopped = registry.call("stop_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "stop_flowgraph", stopped)
            observed = bool(self._workflow.workflow.intent.slots.get("over_air_observed"))
            return self._fold(
                ctx,
                "发射已停止，LightBlue 空口观察已记录。"
                if observed else "发射已停止，但用户未在 LightBlue 中观察到目标广播。",
                source="deterministic-stage",
                ok=bool(stopped.get("ok") and observed),
            )
        if stage_id == "stop_runtime":
            stopped = registry.call("stop_flowgraph", {}, ctx)
            self._record_tool_result(ctx, "stop_flowgraph", stopped)
            return self._fold(
                ctx,
                "硬件 Flowgraph 已停止，运行状态与用户观察结果已记录。",
                source="deterministic-stage",
                ok=bool(stopped.get("ok") and not stopped.get("running")),
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
        ctx: ToolContext, kind: str, result: Dict[str, Any]
    ) -> None:
        ctx.extra.setdefault("events", []).append(
            {"kind": kind, "payload": dict(result or {})}
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
                if not deep_mode:
                    invocations = _stage_executor.synthesize_deterministic_invocations(
                        active, stage, reply
                    )
                if self._tool_ctx is not None:
                    self._tool_ctx.extra["subagent_invocations"] = invocations
                for item in invocations:
                    _store.append_session_event(
                        self.session_id,
                        "subagent_completed",
                        self._workflow_event_payload(
                            {
                                "target_agent": item.get("target_agent"),
                                "task_id": item.get("task_id"),
                                "mode": "deterministic",
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
                reply.needs_confirmation = True
                if missing_completion:
                    reply.text = "{}\n尚未满足 Stage 完成条件：{}。".format(
                        reply.text, ", ".join(missing_completion)
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

    def _digest_with_timeline(self) -> Dict[str, Any]:
        digest = self._workflow.digest()
        digest["timeline"] = _store.recent_events(self.session_id, limit=40)
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
                "local_name": "请说明 BLE Complete Local Name（如 deepradio）。",
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
        else:
            pending_items = list(self._state.coordination.pending_confirmations or [])
            pending = dict(pending_items[-1]) if pending_items else {
                "action": "workflow_checkpoint",
                "reason": stage.checkpoint.reason if stage and stage.checkpoint else "",
                "checkpoint_id": stage.checkpoint.id if stage and stage.checkpoint else "",
                "approved": False,
            }
            text = "当前 Stage 等待你的确认；确认后继续，取消则保留现有工程。"
        return AgentReply(
            text=text,
            stage="CONFIRM",
            needs_confirmation=True,
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
            payload = ev.get("payload", {})
            if isinstance(payload, dict) and payload.get("policy"):
                policy_decisions.append(payload["policy"])
            if isinstance(payload, dict) and payload.get(
                "requires_confirmation"
            ):
                explicit_confirmation = True
            reply.tool_invocations.append(ToolInvocation(
                name=kind, args={}, result=payload,
                ok=bool(payload.get("ok", True) if isinstance(payload, dict)
                        else True)))
            _store.append_session_event(
                self.session_id,
                "tool_called",
                self._workflow_event_payload(
                    {"tool": kind, "result": payload}
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
        state = ctx.extra.get("state")
        if state is not None:
            reply.claims = ClaimStore(state).summary()
            reply.spec_digest = state.spec_digest()
        reply.text = narrative or self._fallback_text(
            {"recipe": None, "metrics": ctx.extra.get("metrics")})
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
        reply.needs_confirmation = (
            False if denied else ((not ok) or needs_policy_response)
        )
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
        reply.needs_confirmation = True
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
