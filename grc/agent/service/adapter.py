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

import logging
import os
import uuid
from typing import Any, Dict, Optional

from ..schema import AgentReply, ToolInvocation
from ..memory.profile import UserProfile
from ..state import ClaimStore, SharedState
from ..tools.registry import ToolContext
from . import orchestrator as _orch
from . import session_store as _store

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
        return {
            "ok": True,
            "version": self._state.project.flowgraph_version,
            "claims": ClaimStore(self._state).summary(),
            "spec_digest": self._state.spec_digest(),
            "canvas_dirty": True,
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
        return {
            "ok": True,
            "snapshot": target,
            "grc_path": restored.project.grc_path,
            "version": restored.project.flowgraph_version,
            "claims": ClaimStore(restored).summary(),
            "spec_digest": restored.spec_digest(),
        }

    # ---- 主入口 ------------------------------------------------------
    def step(self, user_text: str, recipe: str = "",
             simulate: bool = True) -> AgentReply:
        """执行一轮:优先走 deepagents 深度代理,否则确定性降级。"""
        if getattr(self._state, "_load_failed", False):
            backup = getattr(self._state, "_corrupt_backup", "")
            return self._error_reply(
                "会话 SharedState 已损坏，已停止写入以保护原数据。"
                f"备份: {backup or '创建失败'}"
            )
        # 创新 B:自适应档位下,每轮据用户话语平滑更新画像;钉档时 pin 优先。
        if getattr(self.ctx, "adaptive", True):
            try:
                self.profile.observe(user_text)
            except Exception as exc:  # noqa: BLE001
                logger.debug("profile.observe 失败,忽略: %s", exc)
        ctx = self._make_ctx()
        ctx.extra["user_text"] = user_text
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
                reply = AgentReply(
                    text="已取消待执行的工程修改。",
                    stage="CANCELLED",
                    claims=ClaimStore(self._state).summary(),
                    spec_digest=self._state.spec_digest(),
                )
                _store.append_session_event(
                    self.session_id, "confirmation_cancelled", {}
                )
                return reply
            if resolution.get("resolved") and resolution.get("approved"):
                pending = list(self._state.coordination.pending_confirmations)
                last = pending[-1] if pending else {}
                if last.get("action") == "design_link" and last.get("recipe"):
                    reply = self._run_deterministic(
                        ctx, user_text, str(last.get("recipe") or ""),
                        simulate)
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
                    return reply
            if target_recipe and not ctx.extra.get("mutation_forbidden"):
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
                    return self._pending_confirm_reply(proposed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("规格提取失败，继续执行原链路: %s", exc)
        _store.append_session_event(self.session_id, "user_input",
                                    {"text": user_text})
        try:
            agent = _orch.build_agent(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("组装 deepagents 失败,降级到确定性骨架: %s", exc)
            agent = None

        try:
            if agent is not None:
                reply = self._run_deep(agent, ctx, user_text)
            else:
                reply = self._run_deterministic(
                    ctx, user_text, recipe, simulate
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("编排执行异常")
            reply = self._error_reply(
                f"编排出错: {type(exc).__name__}: {exc}"
            )
        try:
            self._state.save(_store.state_path(self.session_id))
        except OSError as exc:
            logger.warning("SharedState 落盘失败: %s", exc)
        return reply

    # ---- 主路径:deepagents ------------------------------------------
    def _run_deep(self, agent: Any, ctx: ToolContext,
                  user_text: str) -> AgentReply:
        config = {"configurable": {"thread_id": self.session_id},
                  "recursion_limit": _recursion_limit()}
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": user_text}]}, config)
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
        # deepagents 把会话文件放在 state 的 "files" 键:镜像到磁盘
        files = result.get("files") if isinstance(result, dict) else None
        if files:
            _store.mirror_session_files(self.session_id, files)
        return self._fold(ctx, narrative, source="deepagents")

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
            _store.append_session_event(self.session_id, kind, payload)

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
        _store.append_session_event(self.session_id, "reply", {
            "source": source, "stage": reply.stage,
            "has_grc": bool(artifacts.get("grc_path"))})
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
        _store.append_session_event(self.session_id, "reply", {
            "source": "policy-propose", "stage": "CONFIRM", "has_grc": False})
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
        return reply

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
