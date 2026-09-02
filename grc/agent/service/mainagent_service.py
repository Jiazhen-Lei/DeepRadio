"""UI-compatible service for the MainAgent-owned dynamic Workflow."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, Optional

from ..memory.profile import UserProfile
from ..schema import AgentReply, ToolInvocation
from ..state import Claim, ClaimStore, Evidence, SharedState, WorkflowDecision
from ..tools.registry import ToolContext
from ..workflow.dynamic import DynamicWorkflowStore
from . import orchestrator as orch
from . import result_projector as projector
from . import session_store as store
from . import subagents

logger = logging.getLogger(__name__)
DEFAULT_RECURSION_LIMIT = 150


def _semantic_hash(path: str) -> str:
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
    try:
        return max(
            1,
            int(
                (os.environ.get("GRC_AGENT_RECURSION_LIMIT") or "").strip()
                or DEFAULT_RECURSION_LIMIT
            ),
        )
    except ValueError:
        return DEFAULT_RECURSION_LIMIT


class _ToolCtxShim:
    def __init__(self, out_dir: str = "") -> None:
        self.out_dir = out_dir


class _CtxShim:
    def __init__(self, agent: "ServiceAgent") -> None:
        self._agent = agent
        self.tool_ctx = _ToolCtxShim()
        self.adaptive = True

    @property
    def profile(self) -> UserProfile:
        return self._agent.profile


class ServiceAgent:
    """Stable GUI facade; MainAgent owns all planning and Stage decisions."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        profile: Any = None,
        platform: Any = None,
    ) -> None:
        self.session_id = session_id or f"gui-{uuid.uuid4().hex[:8]}"
        store.ensure_run_metadata(self.session_id)
        self.profile = profile if isinstance(profile, UserProfile) else UserProfile()
        self._platform = platform
        self._state = SharedState.load(
            store.state_path(self.session_id), session_id=self.session_id
        )
        self._workflow = DynamicWorkflowStore(
            store.workflow_path(self.session_id), subagents.subagent_names()
        )
        self._tool_ctx: Optional[ToolContext] = None
        self._progress_listeners: list[Any] = []
        self.ctx = _CtxShim(self)

    def subscribe_progress(self, callback: Any) -> None:
        if callable(callback) and callback not in self._progress_listeners:
            self._progress_listeners.append(callback)

    def unsubscribe_progress(self, callback: Any) -> None:
        if callback in self._progress_listeners:
            self._progress_listeners.remove(callback)

    def _notify_progress(self, event: str) -> None:
        if not self._progress_listeners:
            return
        snapshot = {
            "event": event,
            "claims": ClaimStore(self._state).summary(),
            "spec_digest": self._state.spec_digest(),
            "workflow_digest": self._digest(),
        }
        for callback in list(self._progress_listeners):
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001
                logger.debug("Progress listener failed", exc_info=True)

    def _make_ctx(self) -> ToolContext:
        export_dir = store.nested_export_dir(
            self.session_id, (self.ctx.tool_ctx.out_dir or "").strip()
        )
        out_dir = os.path.join(store.session_root(self.session_id), "final")
        os.makedirs(out_dir, exist_ok=True)
        if self._platform is None:
            try:
                from .. import env

                self._platform = env.make_platform()
            except Exception as exc:  # noqa: BLE001
                logger.info("GNU Radio platform is unavailable: %s", exc)
        if self._tool_ctx is None:
            self._tool_ctx = ToolContext(platform=self._platform, out_dir=out_dir)
        ctx = self._tool_ctx
        ctx.platform = self._platform
        ctx.out_dir = out_dir
        ctx.extra.update(
            {
                "profile": self.profile,
                "state": self._state,
                "state_path": store.state_path(self.session_id),
                "artifacts": {},
                "events": [],
                "metrics": {},
                "subagent_invocations": [],
                "export_dir": export_dir,
                "session_id": self.session_id,
                "workflow_store": self._workflow,
                "workflow": (
                    self._workflow.workflow.to_dict()
                    if self._workflow.workflow else {}
                ),
                "turn_stage_id": (
                    self._workflow.workflow.current_stage
                    if self._workflow.workflow else ""
                ),
                "mutation_forbidden": False,
                "forbidden_permissions": [],
                "profile_snapshot": self.profile.level,
            }
        )
        ctx.extra.pop("pending_decision", None)
        ctx.extra.pop("_idempotent_results", None)
        ctx.extra.pop("completed_stage_this_turn", None)
        if ctx.flow_graph is None:
            self._load_flowgraph(ctx)
        return ctx

    def _load_flowgraph(self, ctx: ToolContext) -> None:
        path = store.resolve_session_path(
            self.session_id, str(self._state.project.grc_path or "")
        )
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
                    block_id = str(block.params["id"].get_value())
                except Exception:  # noqa: BLE001
                    block_id = str(getattr(block, "name", "") or "")
                if block_id and block_id != "options":
                    ctx.blocks[block_id] = block
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load the session flowgraph %s: %s", path, exc)

    def step(
        self, user_text: str, recipe: str = "", simulate: bool = True
    ) -> AgentReply:
        del recipe, simulate
        if getattr(self._state, "_load_failed", False):
            return self._error_reply("The session state is corrupted; writes were stopped.")
        if self._workflow.load_error:
            return self._error_reply(
                f"The Workflow state is corrupted; writes were stopped. "
                f"{self._workflow.load_error}"
            )
        text = str(user_text or "").strip()
        if not text:
            return self._error_reply("Please describe the radio task.")
        prior_workflow_id = (
            self._workflow.workflow.workflow_id
            if self._workflow.workflow else ""
        )
        self._workflow.begin_turn(text, self._state.project.flowgraph_version)
        if (
            prior_workflow_id
            and self._workflow.workflow
            and self._workflow.workflow.workflow_id != prior_workflow_id
        ):
            self._clear_rf_grant()
        store.append_session_event(
            self.session_id, "user_turn_received", {"text": text}
        )
        self._observe_profile(text)
        return self._invoke_mainagent(text)

    def _invoke_mainagent(self, user_text: str) -> AgentReply:
        ctx = self._make_ctx()
        ctx.extra["user_text"] = user_text
        ctx.extra["workflow"] = self._workflow.workflow.to_dict()
        try:
            agent = orch.build_agent(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("MainAgent assembly failed")
            return self._error_reply(f"MainAgent could not be created: {exc}")
        if agent is None:
            return self._error_reply(
                "MainAgent is unavailable. Configure the LLM and install deepagents; "
                "the production chain no longer switches to a deterministic Workflow."
            )
        current = self._workflow.digest()
        current_stage = self._workflow.current_stage()
        stage_payload = asdict(current_stage) if current_stage else {}
        project = {
            "grc_path": self._state.project.grc_path,
            "project_version": self._state.project.flowgraph_version,
            "config": dict(self._state.project.config),
        }
        specification = self._state.intent.specification.to_dict()
        prompt = (
            f"USER_REQUEST:\n{user_text}\n\n"
            f"CURRENT_WORKFLOW:\n{json.dumps(current, ensure_ascii=False)}\n\n"
            f"CURRENT_STAGE:\n{json.dumps(stage_payload, ensure_ascii=False)}\n\n"
            f"CURRENT_RADIO_SPECIFICATION:\n"
            f"{json.dumps(specification, ensure_ascii=False, default=str)}\n\n"
            f"CURRENT_PROJECT:\n{json.dumps(project, ensure_ascii=False, default=str)}\n\n"
            "请根据以上用户请求和当前状态，读取并遵循 grc-orchestration Skill "
            "处理本轮请求。"
        )
        try:
            from .trace import build_trace_callback

            trace = build_trace_callback(
                session_id=self.session_id,
                context=lambda: {
                    "workflow_id": self._workflow.workflow.workflow_id,
                    "revision": self._workflow.workflow.revision,
                    "stage_id": self._workflow.workflow.current_stage,
                } if self._workflow.workflow else {},
            )
        except ImportError:
            trace = None
        if trace:
            trace.start()
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                {
                    "configurable": {"thread_id": self.session_id},
                    "recursion_limit": _recursion_limit(),
                    "callbacks": [trace] if trace else [],
                },
            )
        except Exception as exc:  # noqa: BLE001
            if trace:
                trace.finish(exc)
            logger.exception("MainAgent execution failed")
            return self._error_reply(f"MainAgent execution failed: {type(exc).__name__}: {exc}")
        if trace:
            trace.finish()
        narrative = self._extract_final_text(result)
        self._record_delegations(result)
        reply = self._fold(ctx, narrative, ok=True)
        self._notify_progress("workflow_updated")
        return reply

    def step_command(self, command: Dict[str, Any]) -> AgentReply:
        action = str((command or {}).get("action") or "")
        if action in {"stop_runtime", "emergency_stop"}:
            return self._stop_runtime(action == "emergency_stop")
        if action == "checkpoint_decision":
            return self._resolve_checkpoint(command)
        if action == "cancel_workflow":
            self._workflow.cancel()
            return self._simple_reply("The current task was cancelled.", "CANCELLED")
        if action in {"retry_stage", "retry_transmit"}:
            workflow = self._workflow.workflow
            if workflow is None:
                return self._error_reply("There is no active Workflow to retry.")
            workflow.execution_status = "running"
            stage = workflow.stage()
            if stage:
                stage.reset()
            self._workflow.save()
            return self._invoke_mainagent("The user requested a retry. Re-check current evidence before acting.")
        if action in {"specification_update", "interaction_response"}:
            return self._invoke_mainagent(
                "The user submitted this structured response: "
                + json.dumps(command, ensure_ascii=False, default=str)
            )
        return self._error_reply(f"Unknown GUI command: {action or '(empty)'}")

    def _resolve_checkpoint(self, command: Dict[str, Any]) -> AgentReply:
        checkpoint_id = str(command.get("checkpoint_id") or "")
        decision = str(command.get("decision") or "")
        try:
            checkpoint = self._workflow.resolve_decision(checkpoint_id, decision)
        except ValueError as exc:
            return self._error_reply(str(exc))
        permission = str(checkpoint.get("permission") or "")
        if decision == "approved" and permission:
            grants = self._state.runtime.granted_permissions
            if permission not in grants:
                grants.append(permission)
            if permission == "rf.start" and self._workflow.workflow is not None:
                self._state.project.config["rf_permission_grant"] = {
                    "workflow_id": self._workflow.workflow.workflow_id,
                    "stage_id": str(checkpoint.get("stage_id") or ""),
                    "project_version": self._state.project.flowgraph_version,
                }
        self._state.decisions.append(
            WorkflowDecision(
                decision_id=f"decision-{uuid.uuid4().hex[:8]}",
                key=f"checkpoint:{checkpoint.get('purpose') or 'user_decision'}",
                value=decision,
                source="gui",
                permission=permission or "project.read",
                workflow_id=(self._workflow.workflow.workflow_id if self._workflow.workflow else ""),
                stage_id=str(checkpoint.get("stage_id") or ""),
            )
        )
        self._state.save(store.state_path(self.session_id))
        store.append_session_event(
            self.session_id,
            "checkpoint_resolved",
            {**checkpoint, "decision": decision},
        )
        if decision == "rejected":
            return self._simple_reply("The requested action was cancelled.", "CANCELLED")
        return self._invoke_mainagent(
            "The user approved the pending decision. Continue the current Workflow "
            "without asking for the same permission again."
        )

    def _stop_runtime(self, emergency: bool) -> AgentReply:
        from ..tools import registry

        ctx = self._make_ctx()
        name = "emergency_stop" if emergency else "stop_flowgraph"
        result = registry.call(name, {}, ctx)
        from .tools_lc import record_tool_event

        record_tool_event(ctx, name, result, {})
        self._state.project.config["rf_armed"] = False
        self._state.project.config.pop("rf_armed_path", None)
        self._clear_rf_grant()
        text = "Emergency stop completed." if emergency else "The RF runtime was stopped."
        return self._fold(ctx, text, ok=bool(result.get("ok", True)))

    def _fold(self, ctx: ToolContext, narrative: str, *, ok: bool) -> AgentReply:
        artifacts = dict(ctx.extra.get("artifacts") or {})
        grc_path = str(artifacts.get("grc_path") or "")
        if grc_path:
            grc_path = store.publish_artifact(self.session_id, grc_path)
            artifacts["grc_path"] = grc_path
            new_hash = _semantic_hash(grc_path)
            old_hash = str(self._state.project.config.get("flowgraph_semantic_hash") or "")
            self._state.project.grc_path = grc_path
            if new_hash and new_hash != old_hash:
                self._state.project.flowgraph_version += 1
                self._state.project.config["flowgraph_semantic_hash"] = new_hash
                ClaimStore(self._state).invalidate_by_version(
                    self._state.project.flowgraph_version
                )
                armed_under_grant = any(
                    event.get("kind") == "arm_hardware_flowgraph"
                    and (event.get("payload") or {}).get("armed")
                    for event in (ctx.extra.get("events") or [])
                )
                binding = dict(
                    self._state.project.config.get("rf_permission_grant") or {}
                )
                if armed_under_grant and binding:
                    binding["project_version"] = self._state.project.flowgraph_version
                    self._state.project.config["rf_permission_grant"] = binding
                elif binding:
                    self._clear_rf_grant()
                if self._workflow.workflow is not None:
                    self._workflow.workflow.base_project_version = (
                        self._state.project.flowgraph_version
                    )
                    self._workflow.save()
        elif self._state.project.grc_path and os.path.isfile(self._state.project.grc_path):
            artifacts["grc_path"] = self._state.project.grc_path

        metrics = dict(ctx.extra.get("metrics") or {})
        if metrics:
            artifacts["metrics"] = metrics

        invocations = []
        for event in ctx.extra.get("events") or []:
            payload = event.get("payload") or {}
            success = not (
                isinstance(payload, dict)
                and (payload.get("ok") is False or payload.get("policy") == "DENY")
            )
            invocations.append(
                ToolInvocation(
                    name=str(event.get("kind") or ""),
                    args=dict(event.get("args") or {}),
                    result=payload if isinstance(payload, dict) else {"result": payload},
                    ok=success,
                )
            )
        scratch = AgentReply(tool_invocations=invocations, artifacts=artifacts)
        projector.project_tool_results(
            self._state,
            scratch,
            record_claim=self._record_claim,
            semantic_hash=_semantic_hash,
        )
        manifest = store.write_artifact_manifest(self.session_id, artifacts)
        artifacts["manifest"] = manifest
        projector.project_artifact_index(
            self._state, manifest, workflow=self._workflow.workflow
        )
        self._state.save(store.state_path(self.session_id))

        checkpoint = dict(
            self._workflow.workflow.checkpoint
            if self._workflow.workflow else {}
        )
        waiting = bool(
            checkpoint and checkpoint.get("status") == "pending"
        )
        approval_waiting = (
            waiting and checkpoint.get("kind", "approval") == "approval"
        )
        pending = (
            {
                **checkpoint,
                "action": checkpoint.get("stage_id") or "workflow_checkpoint",
                "checkpoint_id": checkpoint.get("id") or "",
                "requested_effect": checkpoint.get("permission") or "",
                "can_confirm": True,
                "approved": False,
            }
            if approval_waiting else {}
        )
        status = (
            self._workflow.workflow.execution_status
            if self._workflow.workflow else "errored"
        )
        reply = AgentReply(
            text=narrative.strip() or (
                "The MainAgent completed this turn."
                if ok else "The MainAgent could not complete this turn."
            ),
            stage="WAITING" if waiting else "DELIVER" if ok else "ERROR",
            needs_confirmation=approval_waiting,
            tool_invocations=invocations,
            artifacts=artifacts,
            done=status == "completed",
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            pending=pending,
            workflow_digest=self._digest(),
        )
        store.append_session_event(
            self.session_id,
            "reply",
            {
                "source": "mainagent",
                "stage": reply.stage,
                "has_grc": bool(artifacts.get("grc_path")),
            },
        )
        return reply

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
        version = int(self._state.project.flowgraph_version)
        claim_store = ClaimStore(self._state)
        claim_store.upsert(
            Claim(
                id=claim_id,
                statement=statement,
                layer=layer,
                project_version=version,
                producer=producer,
                measurement_id=measurement_id,
            )
        )
        claim_store.add_evidence(
            claim_id,
            Evidence(
                test=test,
                observation=dict(observation or {}),
                project_version=version,
                artifact=artifact,
                measurement_id=measurement_id,
                evidence_grade=evidence_grade,
            ),
            passed=passed,
        )

    def _digest(self) -> Dict[str, Any]:
        digest = self._workflow.digest()
        digest["project_version"] = self._state.project.flowgraph_version
        digest["timeline"] = store.recent_events(self.session_id, limit=40)
        digest["control_state"] = {
            "current_node": digest.get("current_stage") or "",
            "status": digest.get("execution_status") or "pending",
            "granted_permissions": list(self._state.runtime.granted_permissions),
            "blocker": dict(self._state.runtime.blocker),
            "quality": self._state.runtime.quality,
            "warnings": list(self._state.runtime.warnings),
        }
        runtime = dict(self._state.project.config.get("runtime") or {})
        if runtime:
            deadline = float(runtime.get("deadline") or 0)
            runtime["remaining_seconds"] = (
                max(0.0, deadline - time.time())
                if runtime.get("running") and deadline else 0.0
            )
            runtime["do_not_run_grc"] = True
            digest["runtime"] = runtime
        return digest

    def sync_from_canvas(self, file_path: str) -> Dict[str, Any]:
        path = os.path.abspath(file_path or "")
        current = os.path.abspath(self._state.project.grc_path or "")
        if not path or not current or path != current:
            return {"ok": False, "skipped": True}
        new_hash = _semantic_hash(path)
        old_hash = str(self._state.project.config.get("flowgraph_semantic_hash") or "")
        if new_hash and new_hash == old_hash:
            return {"ok": True, "skipped": True, "unchanged": True}
        self._state.project.flowgraph_version += 1
        self._state.project.config.update(
            {"canvas_dirty": True, "rf_armed": False, "flowgraph_semantic_hash": new_hash}
        )
        self._state.project.config.pop("rf_armed_path", None)
        self._clear_rf_grant()
        ClaimStore(self._state).invalidate_by_version(
            self._state.project.flowgraph_version
        )
        self._workflow.invalidate(self._state.project.flowgraph_version)
        self._tool_ctx = None
        self._state.save(store.state_path(self.session_id))
        return {
            "ok": True,
            "version": self._state.project.flowgraph_version,
            "claims": ClaimStore(self._state).summary(),
            "spec_digest": self._state.spec_digest(),
            "canvas_dirty": True,
            "workflow_digest": self._digest(),
        }

    def bind_opened_project(self, file_path: str) -> Dict[str, Any]:
        path = os.path.abspath(file_path or "")
        if not path or not os.path.isfile(path) or not path.endswith(".grc"):
            return {"ok": False, "skipped": True}
        prior = os.path.abspath(self._state.project.grc_path or "")
        self._state.project.grc_path = path
        self._state.project.config["flowgraph_semantic_hash"] = _semantic_hash(path)
        self._state.project.config["slot_source"] = "canvas"
        if not prior or prior != path:
            self._state.project.flowgraph_version += 1
        self._tool_ctx = None
        self._state.save(store.state_path(self.session_id))
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
        self._state.save(store.state_path(self.session_id))

    def refresh_runtime_capability(self) -> Dict[str, Any]:
        return {
            "claims": ClaimStore(self._state).summary(),
            "spec_digest": self._state.spec_digest(),
            "workflow_digest": self._digest(),
        }

    def record_profile_choice(
        self, *, adaptive: bool, pinned: Optional[str] = None
    ) -> None:
        self.ctx.adaptive = bool(adaptive)
        if pinned:
            self.profile.pin(str(pinned))
        else:
            self.profile.unpin()

    def peek_runtime_digest(self) -> Dict[str, Any]:
        return self._digest()

    def _observe_profile(self, text: str) -> None:
        if not self.ctx.adaptive:
            return
        try:
            self.profile.observe(text)
        except Exception:  # noqa: BLE001
            logger.debug("Profile observation failed", exc_info=True)

    def _clear_rf_grant(self) -> None:
        self._state.project.config.pop("rf_permission_grant", None)
        self._state.runtime.granted_permissions = [
            item
            for item in self._state.runtime.granted_permissions
            if item not in {"rf.start", "RF_RUN"}
        ]

    @staticmethod
    def _extract_final_text(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        for message in reversed(result.get("messages") or []):
            content = getattr(message, "content", None)
            if content is None and isinstance(message, dict):
                content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                text = "".join(
                    str(item.get("text") or "")
                    for item in content if isinstance(item, dict)
                ).strip()
                if text:
                    return text
        return ""

    def _record_delegations(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        for message in result.get("messages") or []:
            calls = getattr(message, "tool_calls", None)
            if calls is None and isinstance(message, dict):
                calls = message.get("tool_calls")
            for call in calls or []:
                if not isinstance(call, dict) or call.get("name") != "task":
                    continue
                args = dict(call.get("args") or {})
                store.append_session_event(
                    self.session_id,
                    "llm_subagent_invoked",
                    {
                        "target_agent": args.get("subagent_type") or "",
                        "description": args.get("description") or "",
                        "call_id": call.get("id") or "",
                    },
                )

    def _simple_reply(self, text: str, stage: str) -> AgentReply:
        return AgentReply(
            text=text,
            stage=stage,
            done=stage in {"CANCELLED", "DELIVER"},
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            workflow_digest=self._digest(),
        )

    def _error_reply(self, message: str) -> AgentReply:
        return AgentReply(
            text=message,
            stage="ERROR",
            done=False,
            claims=ClaimStore(self._state).summary(),
            spec_digest=self._state.spec_digest(),
            workflow_digest=self._digest(),
        )


def build_service_agent(
    session_id: Optional[str] = None,
    profile: Any = None,
    platform: Any = None,
) -> ServiceAgent:
    return ServiceAgent(session_id=session_id, profile=profile, platform=platform)
