"""Execution-routing contracts for the bounded-hybrid V3 architecture."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from grc.agent.service.adapter import ServiceAgent
from grc.agent.state import SharedState


def _stage(
    stage_id="build_ble_advertiser",
    interaction="autonomous",
    recommended=("protocol_agent",),
    completion=("grc_path",),
    execution_mode="hybrid",
    result=None,
):
    return SimpleNamespace(
        id=stage_id,
        interaction=interaction,
        completion=list(completion),
        recommended_agents=list(recommended),
        execution_status="pending",
        resume_pending=False,
        execution_mode=execution_mode,
        allowed_tools=[],
        result=dict(result or {}),
        result_history=[],
    )


class ExecutionRoutingTest(unittest.TestCase):
    def setUp(self):
        self.agent = ServiceAgent(session_id="routing-test", platform=None)
        self.agent._workflow = mock.MagicMock()
        self.ctx = mock.MagicMock()
        self.stage = _stage()

    def test_hybrid_stage_uses_fast_path_before_any_failure(self):
        with mock.patch(
            "grc.agent.service.orchestrator.build_agent",
            return_value=mock.sentinel.agent,
        ) as build, mock.patch.object(
            ServiceAgent, "_run_deep", return_value=mock.sentinel.reply
        ) as deep, mock.patch.object(
            ServiceAgent, "_run_stage_deterministic"
        ) as deterministic:
            reply = self.agent._execute_stage(
                self.ctx, self.stage, "build ble advertiser", "", True
            )
        build.assert_not_called()
        deep.assert_not_called()
        deterministic.assert_called_once()
        self.assertIs(reply, deterministic.return_value)

    def test_hardware_stages_stay_on_host_control_plane(self):
        for stage_id in (
            "hardware_precheck",
            "discover_and_probe_hardware",
            "configure_device",
            "transmit_bounded",
            "stop_and_finalize",
        ):
            mode = (
                "safety_finalizer"
                if stage_id == "stop_and_finalize"
                else "deterministic"
            )
            stage = _stage(
                stage_id=stage_id,
                recommended=("hardware_agent",),
                execution_mode=mode,
            )
            with mock.patch(
                "grc.agent.service.orchestrator.build_agent",
                return_value=mock.sentinel.agent,
            ), mock.patch.object(
                ServiceAgent, "_run_deep", return_value=mock.sentinel.reply
            ) as deep, mock.patch.object(
                ServiceAgent, "_run_stage_deterministic"
            ) as deterministic:
                self.agent._execute_stage(self.ctx, stage, "text", "", True)
            deep.assert_not_called()
            deterministic.assert_called_once()

    def test_hybrid_retry_uses_deepagent_with_new_failure_evidence(self):
        stage = _stage(result={
            "ok": False,
            "outcome": "failed",
            "missing_completion": ["flowgraph_saved"],
        })
        with mock.patch(
            "grc.agent.service.orchestrator.build_agent",
            return_value=mock.sentinel.agent,
        ) as build, mock.patch.object(
            ServiceAgent, "_run_deep", return_value=mock.sentinel.reply
        ) as deep, mock.patch.object(
            ServiceAgent, "_run_stage_deterministic"
        ) as deterministic:
            reply = self.agent._execute_stage(
                self.ctx, stage, "build ble advertiser", "", True
            )
        build.assert_called_once()
        deep.assert_called_once()
        deterministic.assert_not_called()
        self.assertIs(reply, mock.sentinel.reply)

    def test_deterministic_fallback_when_agent_unavailable(self):
        """build_agent -> None (no LLM / no deepagents) downgrades to handler."""
        with mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ), mock.patch.object(
            ServiceAgent, "_run_deep"
        ) as deep, mock.patch.object(
            ServiceAgent,
            "_run_stage_deterministic",
            return_value=mock.sentinel.reply,
        ) as deterministic:
            reply = self.agent._execute_stage(
                self.ctx, self.stage, "text", "", True
            )
        deep.assert_not_called()
        deterministic.assert_called_once()
        self.assertIs(reply, mock.sentinel.reply)

    def test_env_flag_forces_deterministic_without_building_agent(self):
        with mock.patch.dict(
            os.environ, {"DEEPRADIO_EXECUTION_MODE": "deterministic"}
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent"
        ) as build, mock.patch.object(
            ServiceAgent,
            "_run_stage_deterministic",
            return_value=mock.sentinel.reply,
        ):
            reply = self.agent._execute_stage(
                self.ctx, self.stage, "text", "", True
            )
        build.assert_not_called()
        self.assertIs(reply, mock.sentinel.reply)

    def test_checkpoint_still_waits_before_any_executor(self):
        stage = _stage(interaction="checkpoint")
        with mock.patch(
            "grc.agent.service.orchestrator.build_agent"
        ) as build, mock.patch.object(
            ServiceAgent, "_run_stage_deterministic"
        ) as deterministic, mock.patch.object(
            ServiceAgent, "_workflow_waiting_reply",
            return_value=mock.sentinel.reply,
        ) as waiting:
            reply = self.agent._execute_stage(
                self.ctx, stage, "text", "", True
            )
        build.assert_not_called()
        deterministic.assert_not_called()
        waiting.assert_called_once()
        self.assertIs(reply, mock.sentinel.reply)


class TaskCardRoutingTest(unittest.TestCase):
    """TaskCard must expose the Stage's full recommended agent list."""

    def test_task_card_carries_recommended_agents(self):
        from grc.agent.service.stage_executor import make_task_card

        intent = SimpleNamespace(
            raw_text="用 plutosdr 发射 ble 广播",
            slots={"channel": 38},
            capabilities=["ble_advertise"],
            slot_sources={},
            context={},
        )
        workflow = SimpleNamespace(
            workflow_id="wf-route",
            revision=1,
            base_project_version=0,
            task_type="TX_BUILD",
            stages=[],
            intent=intent,
        )
        stage = _stage(
            recommended=("protocol_agent", "verification_agent")
        )
        card = make_task_card(workflow, stage, SharedState(), "text")
        self.assertEqual(
            card.inputs.get("recommended_agents"),
            ["protocol_agent", "verification_agent"],
        )
        self.assertEqual(card.target_agent, "protocol_agent")


class SubagentFilterTest(unittest.TestCase):
    """Stage recommendations filter the assembled Subagent set."""

    def test_subagents_filtered_by_recommendation(self):
        from grc.agent.service import subagents as subs_mod

        ctx = mock.MagicMock()
        ctx.extra = {}
        with mock.patch(
            "grc.agent.service.tools_lc.build_grc_tools", return_value=[]
        ):
            agents = subs_mod.build_grc_subagents(
                ctx, ["protocol_agent"], None
            )
        self.assertEqual([a["name"] for a in agents], ["protocol_agent"])

    def test_subagents_empty_recommendation_keeps_registry(self):
        from grc.agent.service import subagents as subs_mod

        ctx = mock.MagicMock()
        ctx.extra = {}
        with mock.patch(
            "grc.agent.service.tools_lc.build_grc_tools", return_value=[]
        ):
            agents = subs_mod.build_grc_subagents(ctx, [], None)
        self.assertEqual(
            [a["name"] for a in agents], subs_mod.subagent_names()
        )


class ManualTaskTypeContractTest(unittest.TestCase):
    """评测手册 7 个任务的英文输入必须落到设计文档 §5.2 的 Task Type。

    V2 §5.5：复合请求取终态范围最大的候选。Task 1 的英文输入同时含发射、
    信道、接收和观测语义,LLM 判 END_TO_END_SIM 后不得被单向 RX_BUILD 覆盖。
    """

    def _normalize(self, capabilities, current, slots=None):
        from grc.agent.workflow.engine import _task_type_from_capabilities

        return _task_type_from_capabilities(
            list(capabilities), current, slots=dict(slots or {}), forbidden=[]
        )

    def test_task1_end_to_end_survives_capability_normalization(self):
        """Task 1: 收发俱全的仿真请求保持 END_TO_END_SIM(回归 gui-3ae2ace1)。"""
        self.assertEqual(
            self._normalize(
                ["build_signal", "build_tx", "build_rx", "observe"],
                "END_TO_END_SIM",
            ),
            "END_TO_END_SIM",
        )

    def test_single_direction_requests_keep_their_task_type(self):
        """Task 2 / Task 3: 纯单向请求仍分别落 TX_BUILD / RX_BUILD。"""
        self.assertEqual(
            self._normalize(["build_tx"], "TX_BUILD"), "TX_BUILD"
        )
        self.assertEqual(
            self._normalize(["build_rx", "observe"], "RX_BUILD"), "RX_BUILD"
        )

    def test_diagnose_only_request_is_not_swallowed_by_build(self):
        """Task 4: 只诊断请求无 build capability,保持 DIAGNOSE。"""
        self.assertEqual(
            self._normalize(["diagnose", "observe"], "DIAGNOSE"), "DIAGNOSE"
        )

    def test_modify_and_observe_task_types(self):
        """Task 5 / Task 6: 改工程与只观测各自保持任务类型。"""
        self.assertEqual(
            self._normalize(["modify_project"], "MODIFY_PROJECT"),
            "MODIFY_PROJECT",
        )
        self.assertEqual(self._normalize(["observe"], "OBSERVE"), "OBSERVE")

    def test_all_seven_manual_tasks_map_to_expected_task_type(self):
        """手册 7 个任务的实测 capabilities 组合必须落到设计文档 §5.2 的类型。

        每个 case 的 capabilities 都取自真实 LLM 输出(scripts/verify_manual_task.py
        的会话事件),因此这是一张回归网,而不是构造出来的理想输入。
        """
        cases = [
            # (capabilities, LLM 给的 task_type, slots, 期望)
            (["build_signal", "build_tx", "build_rx", "observe"],
             "END_TO_END_SIM", {}, "END_TO_END_SIM"),
            (["build_tx"], "TX_BUILD", {}, "TX_BUILD"),
            # self-contained receiver 带 build_signal 做内部激励,仍是 RX_BUILD
            (["build_rx", "build_signal"], "END_TO_END_SIM", {}, "RX_BUILD"),
            (["build_rx", "diagnose"], "END_TO_END_SIM", {}, "RX_BUILD"),
            (["diagnose", "observe"], "DIAGNOSE", {}, "DIAGNOSE"),
            (["modify_project"], "MODIFY_PROJECT", {}, "MODIFY_PROJECT"),
            # 只观测请求带 diagnose(读指标)不得被 DIAGNOSE 吞掉
            (["realtime_observe", "diagnose"], "OBSERVE", {}, "OBSERVE"),
            (["build_tx", "hardware_configure", "modify_project",
              "hardware_runtime"], "TX_BUILD",
             {"operation": "prepare", "hardware": "plutosdr"},
             "HARDWARE_CONFIGURE"),
        ]
        for capabilities, current, slots, want in cases:
            with self.subTest(capabilities=capabilities):
                self.assertEqual(
                    self._normalize(capabilities, current, slots=slots), want
                )

    def test_hardware_configure_prepare_stays_hardware(self):
        """Task 7: PlutoSDR 配置请求保持 HARDWARE_CONFIGURE。"""
        self.assertEqual(
            self._normalize(
                ["hardware_configure"],
                "HARDWARE_CONFIGURE",
                slots={"operation": "prepare"},
            ),
            "HARDWARE_CONFIGURE",
        )

    def test_task7_prepare_outranks_modify_project(self):
        """Task 7 回归: "配置并停在发射确认" 会同时带出 modify_project /
        build_tx / hardware_runtime,但 operation=prepare 的安全边界必须优先,
        否则丢掉硬件 Stage 与 RF 确认点。"""
        self.assertEqual(
            self._normalize(
                ["build_tx", "hardware_configure", "modify_project",
                 "hardware_runtime"],
                "TX_BUILD",
                slots={"operation": "prepare", "hardware": "plutosdr"},
            ),
            "HARDWARE_CONFIGURE",
        )

    def test_modify_existing_project_is_not_hijacked_by_hardware(self):
        """改既有工程(无 operation=prepare)仍走 MODIFY_PROJECT。"""
        self.assertEqual(
            self._normalize(
                ["modify_project", "hardware_configure", "hardware_runtime"],
                "MODIFY_PROJECT",
            ),
            "MODIFY_PROJECT",
        )


class SubagentCallLimitTest(unittest.TestCase):
    """subagent 轮次限流：不换模型的提速手段(V2 §15.2 循环控制)。"""

    def test_limits_are_attached_without_changing_model(self):
        from grc.agent.service import orchestrator

        subs = [{"name": "verification_agent"}]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRC_AGENT_SUB_MODEL_CALLS", None)
            os.environ.pop("GRC_AGENT_SUB_TOOL_CALLS", None)
            out = orchestrator._apply_sub_limits(subs)
        self.assertTrue(out[0].get("middleware"))
        # 限流只加中间件,绝不覆盖 subagent 的 model(仍复用主模型)。
        self.assertNotIn("model", out[0])

    def test_zero_limits_disable_middleware(self):
        from grc.agent.service import orchestrator

        with mock.patch.dict(
            os.environ,
            {"GRC_AGENT_SUB_MODEL_CALLS": "0", "GRC_AGENT_SUB_TOOL_CALLS": "0"},
        ):
            out = orchestrator._apply_sub_limits([{"name": "spec_agent"}])
        self.assertFalse(out[0].get("middleware"))

    def test_main_agent_also_limited(self):
        """主 Agent 侧也要限轮,否则会反复重新委派(实测 8 次委派)。"""
        from grc.agent.service import orchestrator

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRC_AGENT_MAIN_MODEL_CALLS", None)
            self.assertTrue(orchestrator._main_agent_middleware())
        with mock.patch.dict(os.environ, {"GRC_AGENT_MAIN_MODEL_CALLS": "0"}):
            self.assertFalse(orchestrator._main_agent_middleware())


class TaskTypeReconciliationTest(unittest.TestCase):
    """LLM 判定优先,规则只在候选装不下 capabilities 时才归一化。

    V2 §5.1: task_type 是兼容标签,真正决定执行的是 capabilities 与 Stage
    编排。因此契约是"LLM 说了算,除非执行不下去"。
    """

    def _reconcile(self, llm_task_type, capabilities, slots=None):
        from grc.agent.workflow.engine import _reconcile_task_type

        return _reconcile_task_type(
            llm_task_type, list(capabilities),
            slots=dict(slots or {}), forbidden=[],
        )

    def test_llm_task_type_is_kept_when_it_covers_capabilities(self):
        """手册 Task 6 回归: LLM 判 OBSERVE 不得被 diagnose 规则改写。"""
        self.assertEqual(
            self._reconcile("OBSERVE", ["realtime_observe", "diagnose"],
                            {"direction": "rx"}),
            "OBSERVE",
        )
        self.assertEqual(
            self._reconcile("DIAGNOSE", ["diagnose", "observe"]), "DIAGNOSE"
        )

    def test_normalizes_only_when_candidate_cannot_carry_capabilities(self):
        """手册 Task 7: END_TO_END_SIM/TX_BUILD 没有硬件 Stage 与 RF 确认点,
        装不下 hardware_configure + hardware_runtime,必须归一化。"""
        self.assertEqual(
            self._reconcile(
                "TX_BUILD",
                ["build_tx", "hardware_configure", "modify_project",
                 "hardware_runtime"],
                {"operation": "prepare", "hardware": "plutosdr"},
            ),
            "HARDWARE_CONFIGURE",
        )

    def test_single_direction_keeps_its_acceptance_contract(self):
        """手册 Task 3: RX_BUILD 独有 receive_quality_evaluated(BER)验收,
        LLM 把明确单向请求判成 END_TO_END_SIM 会丢掉该契约。"""
        for direction, want in (("rx", "RX_BUILD"), ("tx", "TX_BUILD")):
            with self.subTest(direction=direction):
                self.assertEqual(
                    self._reconcile(
                        "END_TO_END_SIM", [f"build_{direction}", "build_signal"],
                        {"direction": direction},
                    ),
                    want,
                )

    def test_genuine_end_to_end_is_not_split(self):
        """收发俱全(build_tx + build_rx)才是真端到端,不受 direction 影响。"""
        self.assertEqual(
            self._reconcile(
                "END_TO_END_SIM",
                ["build_signal", "build_tx", "build_rx", "observe"],
                {"direction": "sim"},
            ),
            "END_TO_END_SIM",
        )

    def test_invalid_llm_task_type_falls_back_to_projection(self):
        self.assertEqual(
            self._reconcile("NOT_A_TASK", ["build_tx"], {"direction": "tx"}),
            "TX_BUILD",
        )


class DirectionSlotContractTest(unittest.TestCase):
    """direction 必须落在 tx/rx/sim 三类之一(V2 §6.1 输出契约)。"""

    def _derive(self, capabilities, slots=None):
        from grc.agent.workflow.engine import _apply_semantic_defaults

        out = dict(slots or {})
        sources = {}
        _apply_semantic_defaults(
            out, sources, list(capabilities),
            context={}, requested_operations=[], execution_mode="",
        )
        return out, sources

    def test_baseband_sim_gets_direction_sim(self):
        """Task 1: 基带仿真链路(build_signal 无硬件)派生 direction=sim。"""
        slots, sources = self._derive(["build_signal", "observe"])
        self.assertEqual(slots.get("direction"), "sim")
        self.assertEqual(sources.get("direction"), "derived")

    def test_llm_direction_is_never_overwritten(self):
        """LLM 明确给出的 direction 优先,派生逻辑不得覆盖。"""
        slots, _ = self._derive(["build_signal"], {"direction": "tx"})
        self.assertEqual(slots.get("direction"), "tx")

    def test_hardware_request_does_not_become_sim(self):
        """带硬件的请求不能被派生成仿真。"""
        slots, _ = self._derive(
            ["build_signal"], {"hardware": "plutosdr"}
        )
        self.assertNotEqual(slots.get("direction"), "sim")

    def test_direction_aliases_normalize_to_sim(self):
        from grc.agent.knowledge.spec_requirements import normalize_direction

        for alias in ("transceiver", "simulate", "simulation", "baseband"):
            self.assertEqual(normalize_direction(alias), "sim")


class IdempotentToolCacheTest(unittest.TestCase):
    """同一 Stage 内重复的幂等调用直接回放,省掉一轮工具+LLM。"""

    def _ctx(self):
        ctx = mock.MagicMock()
        ctx.extra = {}
        return ctx

    def test_repeated_idempotent_call_is_replayed(self):
        from grc.agent.service import tools_lc

        ctx = self._ctx()
        with mock.patch(
            "grc.agent.tools.registry.call",
            return_value={"ok": True, "pdu_hex": "02011a"},
        ) as call, mock.patch.object(tools_lc, "record_tool_event"):
            first = tools_lc._call_registry(
                ctx, "build_ble_advertising_pdu", {"local_name": "x"})
            second = tools_lc._call_registry(
                ctx, "build_ble_advertising_pdu", {"local_name": "x"})
        # 真实执行只发生一次
        self.assertEqual(call.call_count, 1)
        self.assertNotIn("repeated_call", json.loads(first))
        replay = json.loads(second)
        self.assertTrue(replay["repeated_call"])
        self.assertEqual(replay["pdu_hex"], "02011a")

    def test_different_arguments_are_not_cached_together(self):
        from grc.agent.service import tools_lc

        ctx = self._ctx()
        with mock.patch(
            "grc.agent.tools.registry.call", return_value={"ok": True}
        ) as call, mock.patch.object(tools_lc, "record_tool_event"):
            tools_lc._call_registry(
                ctx, "build_ble_advertising_pdu", {"local_name": "a"})
            tools_lc._call_registry(
                ctx, "build_ble_advertising_pdu", {"local_name": "b"})
        self.assertEqual(call.call_count, 2)

    def test_write_tools_always_execute(self):
        """部署/打补丁等写操作不得缓存,必须每次真实执行。"""
        from grc.agent.service import tools_lc

        ctx = self._ctx()
        with mock.patch(
            "grc.agent.tools.registry.call", return_value={"ok": True}
        ) as call, mock.patch.object(tools_lc, "record_tool_event"):
            tools_lc._call_registry(ctx, "start_flowgraph", {})
            tools_lc._call_registry(ctx, "start_flowgraph", {})
        self.assertEqual(call.call_count, 2)

    def test_failed_call_is_not_cached(self):
        from grc.agent.service import tools_lc

        ctx = self._ctx()
        with mock.patch(
            "grc.agent.tools.registry.call",
            return_value={"ok": False, "error": "boom"},
        ) as call, mock.patch.object(tools_lc, "record_tool_event"):
            tools_lc._call_registry(ctx, "validate_flowgraph", {})
            tools_lc._call_registry(ctx, "validate_flowgraph", {})
        self.assertEqual(call.call_count, 2)


class TaskCardFactsContractTest(unittest.TestCase):
    """TaskCard 必须携带跨 Stage 所需的结构化事实(V2 §3.6)。

    thread 按 Stage 隔离,subagent 看不到对话原文,事实全靠 TaskCard 传递;
    少一项就会出现"重做前一个 Stage 的活"或"重试时无的放矢"。
    """

    def _card(self, *, stages=(), user_text="switch to channel 38"):
        from grc.agent.service.stage_executor import make_task_card

        intent = SimpleNamespace(
            raw_text="build a ble advertiser on plutosdr",
            slots={"hardware": "plutosdr"},
            capabilities=["protocol"],
            slot_sources={},
            context={},
            missing_slots=[],
            validation_errors=[],
        )
        workflow = SimpleNamespace(
            workflow_id="wf-facts",
            revision=1,
            base_project_version=0,
            task_type="HARDWARE_CONFIGURE",
            stages=list(stages),
            intent=intent,
        )
        stage = _stage(
            stage_id="offline_protocol_verify",
            completion=("ble_packet_valid", "structural_validation_completed"),
        )
        return make_task_card(workflow, stage, SharedState(), user_text)

    def _failed_stage(self, stage_id="build_ble_advertiser"):
        return SimpleNamespace(
            id=stage_id,
            outcome="failed",
            attempt=2,
            result={
                "note": "PDU built but the pluto sink could not be armed.",
                "missing_completion": ["flowgraph_saved"],
                "acceptance": {"failure_codes": ["MISSING_COMPLETION:flowgraph_saved"]},
                "artifacts": {"grc_path": "/tmp/x.grc"},
                "produced_claims": [],
            },
        )

    def test_prior_results_carry_the_failure_note(self):
        """只给 outcome + failure_codes,下游只知道失败、不知道原因。"""
        card = self._card(stages=[self._failed_stage()])
        prior = (card.inputs or {}).get("prior_results") or []
        self.assertTrue(prior)
        self.assertIn("could not be armed", prior[0]["note"])

    def test_completion_status_exposes_satisfied_predicates(self):
        """开工前已成立的谓词要可见,否则会重做前置 Stage 的活。"""
        card = self._card()
        status = (card.inputs or {}).get("completion_status")
        self.assertIsInstance(status, dict)
        # 键必须与本 Stage 的 completion 契约一一对应
        self.assertEqual(
            set(status), {"ble_packet_valid", "structural_validation_completed"}
        )
        self.assertTrue(all(isinstance(v, bool) for v in status.values()))

    def test_last_failure_points_at_the_most_recent_failure(self):
        card = self._card(stages=[self._failed_stage()])
        failure = (card.inputs or {}).get("last_failure") or {}
        self.assertEqual(failure.get("stage_id"), "build_ble_advertiser")
        self.assertEqual(failure.get("attempt"), 2)
        self.assertIn("MISSING_COMPLETION:flowgraph_saved",
                      failure.get("failure_codes") or [])
        self.assertIn("could not be armed", failure.get("note", ""))

    def test_last_failure_is_empty_without_any_failure(self):
        card = self._card()
        self.assertEqual((card.inputs or {}).get("last_failure"), {})

    def test_current_user_text_is_not_replaced_by_first_turn(self):
        """instruction 取首轮 raw_text,本轮原话必须另行传递。"""
        card = self._card(user_text="Please switch transmission to channel 38.")
        self.assertEqual(card.instruction, "build a ble advertiser on plutosdr")
        self.assertEqual(
            (card.inputs or {}).get("current_user_text"),
            "Please switch transmission to channel 38.",
        )


class CheckpointerLifecycleTest(unittest.TestCase):
    """会话历史必须有上限,否则多开会话内存持续增长。"""

    def test_lru_evicts_oldest_sessions(self):
        from grc.agent.service import orchestrator

        saved = orchestrator._CHECKPOINTERS.copy()
        orchestrator._CHECKPOINTERS.clear()
        try:
            for index in range(orchestrator._CHECKPOINTER_LIMIT + 3):
                orchestrator._resolve_checkpointer(f"s{index}", dict)
            self.assertEqual(
                len(orchestrator._CHECKPOINTERS),
                orchestrator._CHECKPOINTER_LIMIT,
            )
            self.assertNotIn("s0", orchestrator._CHECKPOINTERS)
        finally:
            orchestrator._CHECKPOINTERS.clear()
            orchestrator._CHECKPOINTERS.update(saved)

    def test_same_session_reuses_and_release_frees(self):
        from grc.agent.service import orchestrator

        saved = orchestrator._CHECKPOINTERS.copy()
        orchestrator._CHECKPOINTERS.clear()
        try:
            first = orchestrator._resolve_checkpointer("keep", dict)
            again = orchestrator._resolve_checkpointer("keep", dict)
            self.assertIs(first, again)
            orchestrator.release_checkpointer("keep")
            self.assertNotIn("keep", orchestrator._CHECKPOINTERS)
        finally:
            orchestrator._CHECKPOINTERS.clear()
            orchestrator._CHECKPOINTERS.update(saved)


class V3ExecutionGatewayTest(unittest.TestCase):
    def test_stage_scope_and_effect_are_enforced_centrally(self):
        from grc.agent.tools import registry
        from grc.agent.tools.registry import ToolContext

        registry.load_all()
        spec = registry.get("build_ble_advertising_pdu")
        self.assertIsNotNone(spec)
        ctx = ToolContext()
        ctx.extra.update({
            "stage_id": "offline_protocol_verify",
            "stage_allowed_tools": [],
            "stage_effect_level": "ARTIFACT_WRITE",
        })
        denied = registry.call("build_ble_advertising_pdu", {}, ctx)
        self.assertEqual(denied.get("policy"), "DENY")
        self.assertIn("does not authorize", denied.get("error", ""))
        gateway = [
            item for item in (ctx.extra.get("events") or [])
            if item.get("kind") == "execution_gateway"
        ]
        self.assertEqual(gateway[-1]["decision"], "DENY")
        self.assertEqual(gateway[-1]["tool"], "build_ble_advertising_pdu")

        ctx.extra["stage_allowed_tools"] = ["build_ble_advertising_pdu"]
        ctx.extra["stage_effect_level"] = "READ"
        denied = registry.call("build_ble_advertising_pdu", {}, ctx)
        self.assertEqual(denied.get("policy"), "DENY")
        self.assertIn("effect", denied.get("error", "").lower())

    def test_catalog_profiles_only_reference_registered_or_host_macro_tools(self):
        from grc.agent.tools import registry
        from grc.agent.workflow import WorkflowEngine

        engine = WorkflowEngine(
            os.path.join(os.path.dirname(__file__), "_missing_workflow.yaml"),
            catalog_path=os.path.join(
                os.path.dirname(__file__), "..", "workflow", "task_catalog.yaml"
            ),
        )
        registry.load_all()
        known = set(registry.names()) | {"design_flowgraph"}
        profiles = engine.catalog.get("stage_profiles") or {}
        self.assertTrue(profiles)
        for stage_id, profile in profiles.items():
            with self.subTest(stage_id=stage_id):
                self.assertIn(profile.get("execution_mode"), {
                    "agentic", "hybrid", "deterministic", "checkpoint",
                    "safety_finalizer",
                })
                self.assertLessEqual(
                    set(profile.get("allowed_tools") or []), known
                )

    def test_offline_retry_never_refreshes_hardware(self):
        from grc.agent.tools import registry

        agent = ServiceAgent(session_id="offline-retry-scope", platform=None)
        agent._workflow = mock.MagicMock()
        agent._workflow.workflow = SimpleNamespace(
            intent=SimpleNamespace(
                capabilities=["protocol", "hardware_configure"], slots={}
            )
        )
        stage = SimpleNamespace(
            id="offline_protocol_verify", effect_level="ARTIFACT_WRITE"
        )
        with mock.patch.object(registry, "call") as called:
            self.assertEqual(agent._refresh_hardware_for_retry(stage), "")
        called.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
