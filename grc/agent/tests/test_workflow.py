import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from grc.agent.schema import AgentReply, ToolInvocation
from grc.agent.service.stage_executor import (
    bind_invocation_result,
    make_invocation_card,
    make_result_envelope,
    make_task_card,
    synthesize_deterministic_invocations,
)
from grc.agent.state import Claim, ClaimStore, Evidence, SharedIntent, SharedState
from grc.agent.workflow import WorkflowEngine
from grc.agent.workflow.intent_alignment import IntentAlignmentCoordinator
from grc.agent.workflow.revision import analyze_intent_patch


class WindowsPersistenceContractTest(unittest.TestCase):
    def test_workflow_atomic_replace_retries_short_file_lock(self):
        from grc.agent.workflow import engine as engine_module

        replace = mock.Mock(
            side_effect=[PermissionError("locked"), PermissionError("locked"), None]
        )
        with mock.patch.object(engine_module.os, "replace", replace), mock.patch.object(
            engine_module.time, "sleep"
        ) as sleep:
            engine_module._atomic_replace("workflow.tmp", "workflow.yaml")
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_workflow_atomic_replace_propagates_persistent_lock(self):
        from grc.agent.workflow import engine as engine_module

        with mock.patch.object(
            engine_module.os, "replace", side_effect=PermissionError("locked")
        ), mock.patch.object(engine_module.time, "sleep"):
            with self.assertRaises(PermissionError):
                engine_module._atomic_replace(
                    "workflow.tmp", "workflow.yaml", retries=2
                )


class UserFacingNarrationTest(unittest.TestCase):
    def test_offline_design_reply_is_friendly_and_hides_gui_internals(self):
        from grc.agent.tools.narrate import narrate_design

        recipe = mock.Mock(
            title="QPSK Transmitter", difficulty="T2", knobs={}
        )
        text = narrate_design(
            recipe,
            {"valid": True, "num_blocks": 6, "metrics": {}},
            mock.Mock(level="student"),
        )
        self.assertIn("✅", text)
        self.assertIn("passed validation", text)
        self.assertIn("No hardware was accessed", text)
        self.assertNotIn("QT GUI", text)
        self.assertNotIn("File Sink", text)
        self.assertNotIn("headless", text)


class SemanticControlPlaneContractTest(unittest.TestCase):
    def test_llm_readonly_false_cannot_override_host_forbidden_policy(self):
        from types import SimpleNamespace
        from grc.agent.service.adapter import ServiceAgent

        agent = ServiceAgent.__new__(ServiceAgent)
        workflow = SimpleNamespace(
            task_type="MODIFY_PROJECT",
            current_stage="apply_and_verify",
            intent=WorkflowIntent(
                context={
                    "forbidden_capabilities": ["modify_project"],
                    "turn_semantics": {"read_only": False},
                }
            ),
        )
        ctx = SimpleNamespace(extra={})
        agent._refresh_mutation_gate(ctx, "change it", workflow)
        self.assertTrue(ctx.extra["mutation_forbidden"])

    def test_semantic_readonly_tightens_an_otherwise_writable_turn(self):
        from types import SimpleNamespace
        from grc.agent.service.adapter import ServiceAgent

        agent = ServiceAgent.__new__(ServiceAgent)
        workflow = SimpleNamespace(
            task_type="MODIFY_PROJECT",
            current_stage="inspect_and_plan",
            intent=WorkflowIntent(
                context={"turn_semantics": {"read_only": True}}
            ),
        )
        ctx = SimpleNamespace(extra={})
        agent._refresh_mutation_gate(ctx, "inspect it", workflow)
        self.assertTrue(ctx.extra["mutation_forbidden"])

    def test_recipe_switch_reads_canonical_llm_target(self):
        from types import SimpleNamespace
        from grc.agent.service.adapter import ServiceAgent

        agent = ServiceAgent.__new__(ServiceAgent)
        agent._state = SharedState()
        agent._state.project.grc_path = "/tmp/current.grc"
        agent._state.project.config.update({
            "recipe": "bpsk_awgn", "modulation": "bpsk",
        })
        workflow = SimpleNamespace(
            intent=WorkflowIntent(
                context={
                    "turn_semantics": {
                        "recipe_switch_target": "qpsk_awgn",
                    }
                }
            )
        )
        target, already = agent._semantic_recipe_switch(workflow)
        self.assertEqual(target, "qpsk_awgn")
        self.assertEqual(already, "")


class PlanCoverageAndRelationContractTest(unittest.TestCase):
    """P0 contracts: plan coverage binding, LLM turn relation, archival."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def _pluto_workflow(self, session_id):
        state = SharedState(session_id=session_id)
        engine = self.engine()
        workflow = engine.consume_turn(
            "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，"
            "保存配置并停在发射确认。",
            state,
        )
        return engine, workflow, state

    def test_planner_tool_actions_bind_missing_catalog_stages(self):
        """V4 regression: BLE planner actions must not be silently dropped."""
        from grc.agent.workflow.plan_compiler import compile_stages

        engine, workflow, state = self._pluto_workflow("coverage-bind")
        stage = engine.start_stage()
        intent = workflow.intent
        composed = [item.id for item in workflow.stages]
        self.assertNotIn("build_ble_advertiser", composed)
        # The planner proposed BLE builder tool actions for a generic TX plan.
        proposal = [
            {"id": "build_ble_advertising_pdu", "objective": "BLE PDU"},
            {"id": "generate_ble_1m_waveform", "objective": "BLE waveform"},
            {"id": "verify_ble_packet_bits", "objective": "BLE verify"},
        ]
        stages = [
            engine.catalog["task_candidates"]["TX_BUILD"]["stages"][0]
        ]
        stages = [type(stage).from_dict(dict(s)) for s in stages]
        compiled, _nodes, rejected, unbound = compile_stages(
            intent, stages, catalog=engine.catalog, proposal=proposal
        )
        self.assertEqual(rejected, [])
        self.assertEqual(unbound, [])
        ids = [item.id for item in compiled]
        self.assertIn("build_ble_advertiser", ids)
        self.assertIn("offline_protocol_verify", ids)
        # Bound stages must execute before the next decision boundary.
        checkpoint_index = next(
            (
                index for index, item in enumerate(compiled)
                if "checkpoint" in str(getattr(item, "interaction", "") or "")
            ),
            len(compiled),
        )
        self.assertLess(ids.index("build_ble_advertiser"), checkpoint_index)

    def test_unbound_rf_action_blocks_instantiation(self):
        from grc.agent.workflow.plan_compiler import PlanCoverageError

        engine, workflow, state = self._pluto_workflow("coverage-block")
        intent = workflow.intent
        node = type(
            "Node", (), {
                "id": "arm_hardware_flowgraph",
                "stage_id": "arm_hardware_flowgraph",
                "objective": "",
                "requires": [],
                "produces": [],
                "success_predicates": [],
                "tools": [],
            },
        )()
        with mock.patch(
            "grc.agent.workflow.llm_planner.propose_plan",
            return_value=[{"id": "dummy_action"}],
        ), mock.patch(
            "grc.agent.workflow.engine.plan_needs_proposal",
            return_value=True,
        ), mock.patch(
            "grc.agent.workflow.plan_compiler.validate_proposal",
            return_value=([node], []),
        ), mock.patch(
            "grc.agent.workflow.plan_compiler.ensure_plan_coverage",
            return_value=(list(workflow.stages), [], ["arm_hardware_flowgraph"]),
        ):
            with self.assertRaises(PlanCoverageError):
                engine.instantiate(intent, state)

    def test_llm_relation_adjustment_does_not_supersede(self):
        """V4 regression: 'local name must be X' is an adjustment, not a new task."""
        engine, workflow, state = self._pluto_workflow("relation-adjust")
        engine.start_stage()
        original_id = workflow.workflow_id

        def chat_side_effect(messages, *args, **kwargs):
            system = str(messages[0].get("content") or "")
            if "会话语义解析器" in system:
                return json.dumps(
                    {
                        "relation": "adjustment",
                        "reason": "parameter supplement",
                        "intent_patch": {
                            "slots": {"local_name": "cindysha"},
                            "capabilities": [],
                        },
                    }
                )
            raise AssertionError("The turn must use exactly one semantic LLM call")

        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.chat", side_effect=chat_side_effect,
        ) as chat:
            engine.consume_turn(
                "The local name of the signal ble must be 'cindysha'.", state
            )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(engine.workflow.workflow_id, original_id)
        self.assertEqual(
            engine.workflow.intent.slots.get("local_name"), "cindysha"
        )

    def test_relation_llm_receives_checkpoint_state_for_transmit_confirmation(self):
        from grc.agent.workflow.schema import Checkpoint, Stage, Workflow

        engine = self.engine()
        stage = Stage(
            id="rf_plan_confirmation",
            interaction="checkpoint",
            execution_status="waiting",
            checkpoint=Checkpoint(
                id="cp-rf-plan",
                reason="Approve bounded RF transmission",
                requested_effect="RF_RUN",
            ),
            transitions={"approved": "completed", "rejected": "cancelled"},
        )
        engine.workflow = Workflow(
            workflow_id="wf-confirm",
            task_type="HARDWARE_CONFIGURE",
            intent=WorkflowIntent(raw_text="Transmit BLE"),
            stages=[stage],
            execution_status="waiting",
            current_stage=stage.id,
        )
        response = json.dumps({
            "relation": "approval",
            "reason": "The user approves the current RF checkpoint",
            "turn_semantics": {
                "read_only": False,
                "confirmation_decision": "approved",
                "recipe_switch_target": "",
            },
        })
        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.chat", return_value=response
        ) as chat:
            relation = engine._turn_relation_llm("confirm to transmit")
        self.assertEqual(relation, "approval")
        payload = json.loads(chat.call_args.args[0][1]["content"])
        self.assertTrue(payload["current_workflow"]["has_checkpoint"])
        self.assertEqual(
            payload["current_workflow"]["stage_status"], "waiting"
        )

    def test_llm_unreachable_raises_instead_of_rule_fallback(self):
        engine, workflow, state = self._pluto_workflow("relation-outage")
        engine.start_stage()
        engine._activate_current()
        with mock.patch(
            "grc.agent.llm.is_configured", return_value=False
        ), mock.patch(
            "grc.agent.llm.intent_test_bypass_enabled", return_value=False
        ):
            with self.assertRaises(Exception) as raised:
                engine.consume_turn("继续调整一下参数", state)
        self.assertIn("language model", str(raised.exception).lower())

    def test_superseded_workflow_is_archived_as_previous_attempt(self):
        engine, workflow, state = self._pluto_workflow("archive")
        engine.start_stage()
        old_id = workflow.workflow_id
        old_task_type = workflow.task_type

        def chat_side_effect(messages, *args, **kwargs):
            system = str(messages[0].get("content") or "")
            if "会话关系判定器" in system:
                return json.dumps({"relation": "new_task", "reason": "different goal"})
            return json.dumps({
                "task_type": "END_TO_END_SIM",
                "confidence": 0.9,
                "capabilities": ["build_signal"],
                "slots": {"modulation": "bpsk"},
            })

        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.chat", side_effect=chat_side_effect,
        ), mock.patch(
            "grc.agent.workflow.llm_planner.propose_plan",
            return_value=None,
        ):
            engine.consume_turn("构建 BPSK AWGN 并测 EVM", state)
        new_workflow = engine.workflow
        self.assertNotEqual(new_workflow.workflow_id, old_id)
        self.assertTrue(new_workflow.previous_attempts)
        archived = new_workflow.previous_attempts[-1]
        self.assertEqual(archived["workflow_id"], old_id)
        self.assertEqual(archived["task_type"], old_task_type)
        self.assertTrue(archived["stages"])
        digest = engine.digest()
        self.assertTrue(digest.get("previous_attempts"))

    def test_rule_entities_do_not_leak_beside_llm_values(self):
        """V4 regression: entities must not keep the regex guess 'of'."""
        from grc.agent.workflow.engine import complete_intent

        rules = WorkflowIntent(
            raw_text="The local name of the signal ble must be 'cindysha'",
            task_type="TX_BUILD",
            confidence=0.65,
            slots={"local_name": "of"},
            slot_sources={"local_name": "rules"},
            missing_slots=[],
        )
        rules.entities = {"local_name": "of"}
        payload = {
            "task_type": "TX_BUILD",
            "confidence": 0.95,
            "capabilities": ["build_tx"],
            "slots": {"local_name": "cindysha"},
        }
        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.chat",
            return_value=json.dumps(payload),
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots["local_name"], "cindysha")
        self.assertEqual(completed.entities.get("local_name"), "cindysha")


class ExternalWaitingAndFullPlanTest(unittest.TestCase):
    """V4 regression contracts.

    * A stage that only misses external hardware preconditions parks as
      ``waiting`` with a friendly note instead of a hard ``failed`` verdict.
    * The digest always exposes the whole plan, deferred stages included.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def _pluto_workflow(self, session_id):
        state = SharedState(session_id=session_id)
        engine = self.engine()
        workflow = engine.consume_turn(
            "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，"
            "保存配置并停在发射确认。",
            state,
        )
        return engine, workflow

    def test_external_precondition_miss_parks_stage_waiting(self):
        engine, workflow = self._pluto_workflow("external-wait")
        stage = engine.start_stage()
        stage.completion = ["hardware_endpoint_present"]
        accepted = engine.accept_result({
            "ok": True,
            "stage_id": stage.id,
            "workflow_revision": workflow.revision,
            "base_project_version": workflow.base_project_version,
            "completion": {"hardware_endpoint_present": False},
        })
        self.assertTrue(accepted)
        self.assertEqual(stage.execution_status, "waiting")
        self.assertEqual(stage.outcome, "inconclusive")
        self.assertEqual(workflow.execution_status, "waiting")
        self.assertIn("Waiting on hardware", str(stage.result.get("note")))

    def test_missing_execution_product_still_fails(self):
        engine, workflow = self._pluto_workflow("real-failure")
        stage = engine.start_stage()
        stage.completion = ["flowgraph_saved"]
        engine.accept_result({
            "ok": True,
            "stage_id": stage.id,
            "workflow_revision": workflow.revision,
            "base_project_version": workflow.base_project_version,
            "completion": {"flowgraph_saved": False},
        })
        self.assertEqual(stage.outcome, "failed")

    def test_digest_shows_full_plan_including_deferred(self):
        engine, workflow = self._pluto_workflow("full-plan")
        digest = engine.digest()
        ids = [str(item.get("id")) for item in digest["stages"]]
        deferred_ids = [
            str(item.get("id")) for item in (workflow.deferred_plan or [])
        ]
        self.assertTrue(deferred_ids)
        for deferred_id in deferred_ids:
            self.assertIn(deferred_id, ids)
        deferred_rows = [
            item for item in digest["stages"]
            if item.get("execution_status") == "deferred"
        ]
        self.assertEqual(len(deferred_rows), len(deferred_ids))
        self.assertEqual(
            digest.get("stage_total"),
            len(workflow.stages) + len(deferred_ids),
        )


class DynamicWorkflowV2ContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def test_new_workflow_resets_runtime_quality_not_historical_claims(self):
        state = SharedState(session_id="quality-reset")
        state.runtime.quality = "warning"
        state.runtime.warnings = [{"code": "old_run_warning"}]
        state.claims.append(Claim(
            id="historical_failure",
            statement="Prior run had a warning",
            layer="hardware",
            status="Failed",
            project_version=0,
        ))

        self.engine().consume_turn("构建 BPSK AWGN 并测 EVM", state)

        self.assertEqual(state.runtime.quality, "clean")
        self.assertEqual(state.runtime.warnings, [])
        self.assertEqual(state.claims[0].status, "Failed")

    def test_completion_is_a_hard_gate(self):
        state = SharedState(session_id="completion")
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", state)
        stage = engine.start_stage()
        card = make_task_card(workflow, stage, state, workflow.intent.raw_text)
        reply = AgentReply(text="已经完成", stage="FINAL")
        envelope = make_result_envelope(workflow, stage, state, card, reply, [])
        self.assertFalse(envelope.ok)
        self.assertFalse(envelope.completion["flowgraph_saved"])
        self.assertFalse(envelope.completion["structural_validation_completed"])

    def test_empty_invocations_fail_protocol_gate(self):
        path = self.root / "radio.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="protocol-empty")
        state.project.grc_path = str(path)
        state.project.flowgraph_version = 1
        state.claims.append(
            Claim(
                id="structure",
                statement="structure valid",
                layer="structure",
                status="Pass",
                project_version=1,
            )
        )
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", state)
        stage = engine.start_stage()
        card = make_task_card(workflow, stage, state, workflow.intent.raw_text)
        reply = AgentReply(
            text="完成",
            stage="FINAL",
            artifacts={"grc_path": str(path), "metrics": {"evm_pct": 2.0}},
            tool_invocations=[
                ToolInvocation(
                    name="validate_flowgraph",
                    args={},
                    result={"ok": True, "valid": True},
                    ok=True,
                )
            ],
        )
        envelope = make_result_envelope(workflow, stage, state, card, reply, [])
        self.assertFalse(envelope.ok)

    def test_completion_accepts_current_artifacts_and_evidence(self):
        path = self.root / "radio.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="completion-ok")
        state.project.grc_path = str(path)
        state.project.flowgraph_version = 1
        state.claims.append(
            Claim(
                id="structure",
                statement="structure valid",
                layer="structure",
                status="Pass",
                project_version=1,
            )
        )
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", state)
        stage = engine.start_stage()
        card = make_task_card(workflow, stage, state, workflow.intent.raw_text)
        reply = AgentReply(
            text="完成",
            stage="FINAL",
            artifacts={"grc_path": str(path), "metrics": {"evm_pct": 2.0}},
            tool_invocations=[
                ToolInvocation(
                    name="design_link",
                    args={},
                    result={"ok": True, "valid": True, "recipe": "bpsk_awgn"},
                    ok=True,
                ),
                ToolInvocation(
                    name="validate_flowgraph",
                    args={},
                    result={"ok": True, "valid": True},
                    ok=True,
                )
            ],
        )
        envelope = make_result_envelope(
            workflow,
            stage,
            state,
            card,
            reply,
            synthesize_deterministic_invocations(card, stage, reply),
        )
        self.assertTrue(envelope.ok)
        self.assertTrue(all(envelope.completion.values()))
        self.assertTrue(envelope.invocations)
        self.assertEqual(len(envelope.invocations), 1)
        self.assertEqual(
            envelope.invocations[0]["target_agent"],
            "deterministic_stage_handler",
        )
        self.assertTrue(all(item.get("protocol_valid") for item in envelope.invocations))

    def test_deep_invocation_envelope_is_per_subagent(self):
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", SharedState())
        stage = engine.start_stage()
        parent = make_task_card(workflow, stage, SharedState(), workflow.intent.raw_text)
        cards = [
            make_invocation_card(parent, "radio_design_agent"),
            make_invocation_card(parent, "flowgraph_agent"),
        ]
        valid = bind_invocation_result(
            vars(cards[0]),
            {
                "task_id": cards[0].task_id,
                "ok": True,
                "outcome": "passed",
                "artifacts": {},
                "completion": {},
                "workflow_id": parent.workflow_id,
                "stage_id": parent.stage_id,
                "workflow_revision": parent.workflow_revision,
                "base_project_version": parent.base_project_version,
            },
            parent,
        )
        invalid = bind_invocation_result(
            vars(cards[1]),
            {"task_id": "other", "ok": True, "workflow_id": parent.workflow_id},
            parent,
        )
        self.assertTrue(valid["protocol_valid"])
        self.assertFalse(invalid["protocol_valid"])
        self.assertEqual(valid["target_agent"], "radio_design_agent")
        self.assertEqual(invalid["target_agent"], "flowgraph_agent")

    def test_missing_recommended_agent_does_not_override_completion(self):
        path = self.root / "radio.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="protocol-partial")
        state.project.grc_path = str(path)
        state.project.flowgraph_version = 1
        state.claims.append(
            Claim(
                id="structure",
                statement="structure valid",
                layer="structure",
                status="Pass",
                project_version=1,
            )
        )
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", state)
        stage = engine.start_stage()
        parent = make_task_card(workflow, stage, state, workflow.intent.raw_text)
        reply = AgentReply(
            text="完成",
            stage="FINAL",
            artifacts={"grc_path": str(path), "metrics": {"evm_pct": 2.0}},
            tool_invocations=[
                ToolInvocation(
                    name="design_link",
                    args={},
                    result={"ok": True, "valid": True, "recipe": "bpsk_awgn"},
                    ok=True,
                ),
                ToolInvocation(
                    name="validate_flowgraph",
                    args={},
                    result={"ok": True, "valid": True},
                    ok=True,
                )
            ],
        )
        agents = list(stage.recommended_agents or [parent.target_agent])
        card = make_invocation_card(parent, agents[0])
        only = bind_invocation_result(
            vars(card),
            {
                "task_id": card.task_id,
                "ok": True,
                "outcome": "passed",
                "artifacts": {},
                "completion": {},
                "workflow_id": parent.workflow_id,
                "stage_id": parent.stage_id,
                "workflow_revision": parent.workflow_revision,
                "base_project_version": parent.base_project_version,
            },
            parent,
        )
        if len(agents) < 2:
            only["target_agent"] = "unrelated_agent"
        envelope = make_result_envelope(
            workflow, stage, state, parent, reply, [only]
        )
        self.assertTrue(envelope.ok)

    def test_alignment_accepts_bare_ebn0_followup(self):
        state = SharedState(session_id="rx-ebn0")
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK 接收机并测 BER", state)
        self.assertEqual(workflow.current_stage, "rx_spec_alignment")
        self.assertIn("ebn0_db", workflow.intent.missing_slots)
        workflow_id = workflow.workflow_id
        resumed = engine.consume_turn("8dB", state)
        self.assertEqual(resumed.workflow_id, workflow_id)
        self.assertEqual(resumed.intent.slots.get("ebn0_db"), 8.0)
        self.assertNotIn("ebn0_db", resumed.intent.missing_slots)
        self.assertEqual(resumed.current_stage, "rx_build_and_verify")

    def test_feedback_keeps_workflow_identity(self):
        state = SharedState(session_id="feedback")
        engine = self.engine()
        workflow = engine.consume_turn("配置 SDR 硬件", state)
        workflow_id = workflow.workflow_id
        engine.start_stage()
        stage = engine.current_stage()
        engine.accept_result(
            {
                "workflow_id": workflow_id,
                "stage_id": stage.id,
                "workflow_revision": workflow.revision,
                "base_project_version": workflow.base_project_version,
                "ok": False,
                "outcome": "failed",
                "completion": {name: False for name in stage.completion},
            }
        )
        with mock.patch.object(engine, "_turn_relation", return_value="answer"):
            resumed = engine.consume_turn(
                "使用 USRP B210，中心频率 2.4 GHz，采样率 1 Msps", state
            )
        self.assertEqual(resumed.workflow_id, workflow_id)
        self.assertEqual(resumed.intent.turn_relation, "answer")
        self.assertEqual(resumed.intent.missing_slots, [])
        self.assertEqual(resumed.intent.raw_text, "配置 SDR 硬件")

    def test_short_feedback_does_not_pollute_capabilities_or_raw_text(self):
        state = SharedState(session_id="feedback-stable")
        engine = self.engine()
        workflow = engine.consume_turn(
            "用 Pluto 发射 BLE，local name 为 deepradio", state
        )
        original_text = workflow.intent.raw_text
        original_capabilities = list(workflow.intent.capabilities)
        stage = engine.start_stage()
        engine.accept_result({
            "workflow_id": workflow.workflow_id,
            "stage_id": stage.id,
            "workflow_revision": workflow.revision,
            "base_project_version": workflow.base_project_version,
            "ok": False,
            "outcome": "failed",
            "completion": {name: False for name in stage.completion},
        })
        resumed = engine.consume_turn("确认", state)
        self.assertEqual(resumed.intent.raw_text, original_text)
        self.assertEqual(resumed.intent.capabilities, original_capabilities)

    def test_compound_hardware_request_inserts_build_stage(self):
        engine = self.engine()
        workflow = engine.consume_turn(
            "构建 BPSK 信号并配置 USRP B210，中心频率 2.4 GHz，采样率 1 Msps",
            SharedState(session_id="compound"),
        )
        self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
        self.assertEqual(workflow.stages[0].id, "build_and_verify")

    def test_project_version_rebases_pending_workflow(self):
        engine = self.engine()
        workflow = engine.consume_turn(
            "构建 BPSK AWGN", SharedState(session_id="rebase")
        )
        revision = workflow.revision
        engine.reconcile_project_version(5)
        self.assertEqual(workflow.base_project_version, 5)
        self.assertEqual(workflow.revision, revision + 1)

    def test_session_events_have_seq_and_control_plane_fields(self):
        import json
        from unittest import mock

        from grc.agent.service import session_store as store

        with mock.patch.object(store, "sessions_root", return_value=str(self.root)):
            store.append_session_event(
                "evt-session",
                "user_turn_received",
                {
                    "text": "hi",
                    "workflow_id": "wf-1",
                    "workflow_revision": 1,
                    "task_type": "END_TO_END_SIM",
                    "stage_id": "build_and_verify",
                    "attempt": 1,
                    "profile_level": "student",
                },
            )
            store.append_session_event(
                "evt-session",
                "reply",
                {
                    "workflow_id": "wf-1",
                    "stage_id": "build_and_verify",
                    "attempt": 1,
                },
            )
        lines = (
            (self.root / "evt-session" / "events.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(first["workflow_id"], "wf-1")
        self.assertEqual(first["stage_id"], "build_and_verify")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(first["task_type"], "END_TO_END_SIM")
        self.assertEqual(first["payload"]["workflow_id"], "wf-1")
        self.assertEqual(second["workflow_id"], "wf-1")


# --- test_workflow_capability_composition.py ---

import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

from grc.agent.schema import AgentReply, ToolInvocation
from grc.agent.state import SharedState
from grc.agent.workflow import WorkflowEngine
from grc.agent.workflow.completion import evaluate
from grc.agent.tools.hardware_tools import _device_command
from grc.agent.tools.hardware_profiles import resolve_hardware_profile


def project_state() -> SharedState:
    state = SharedState(session_id="capability-test")
    state.project.grc_path = "/tmp/rx_bpsk_awgn.grc"
    state.project.config.update({
        "recipe": "rx_bpsk_awgn",
        "modulation": "bpsk",
        "channel": "awgn",
    })
    return state


class WorkflowCapabilityCompositionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self) -> WorkflowEngine:
        return WorkflowEngine(str(self.root / "workflow.json"))

    def hardware_rx_text(self, with_rate: bool = False) -> str:
        suffix = "，采样率2 Msps" if with_rate else ""
        return "使用usrpb210构建接收机，在2.402GHz绘制出实时的频谱图" + suffix

    def test_decimal_frequency_is_not_truncated_after_chinese_text(self):
        intent = self.engine().classify(self.hardware_rx_text(), SharedState())
        self.assertEqual(intent.slots["carrier_frequency"], 2_402_000_000.0)
        self.assertNotIn("carrier_frequency_out_of_device_range", intent.validation_errors)

    def test_project_identifier_does_not_become_user_signal_parameters(self):
        text = (
            "直接修改rx_bpsk_awgn，使其使用usrpb210构建接收机，"
            "在2.402GHz绘制实时频谱"
        )
        intent = self.engine().classify(text, project_state())
        self.assertEqual(intent.task_type, "MODIFY_PROJECT")
        self.assertEqual(intent.slots["modulation"], "")
        self.assertEqual(intent.slots["channel"], "")
        self.assertNotIn("modulation", intent.slot_sources)
        self.assertEqual(
            intent.context["current_project"]["modulation"], "bpsk"
        )

    def test_compound_request_keeps_independent_capabilities(self):
        workflow = self.engine().consume_turn(
            self.hardware_rx_text(with_rate=True), SharedState()
        )
        self.assertEqual(workflow.task_type, "RX_BUILD")
        self.assertTrue({
            "build_rx", "hardware_configure", "observe", "realtime_observe"
        }.issubset(workflow.intent.capabilities))
        self.assertEqual(
            workflow.intent.slots["signal_source_scope"], "live_device"
        )

    def test_offline_observation_and_generated_fixture_are_distinguished(self):
        offline = self.engine().classify(
            "查看当前工程的频谱和星座图，只观察不修改",
            project_state(),
        )
        self.assertEqual(
            offline.slots["signal_source_scope"], "current_project_offline"
        )
        fixture = self.engine().classify(
            "构建 BPSK 接收机并测 BER", SharedState()
        )
        self.assertEqual(
            fixture.slots["signal_source_scope"], "generated_fixture"
        )

    def test_pluto_live_receive_observation_uses_hardware_rx_path(self):
        intent = self.engine().classify(
            "查看当前 PlutoSDR 天线口接收信号的实时频谱，"
            "中心频率 2.402 GHz，采样率 2 Msps",
            SharedState(),
        )
        self.assertEqual(intent.task_type, "RX_BUILD")
        self.assertEqual(intent.slots["direction"], "rx")
        self.assertEqual(intent.slots["signal_source_scope"], "live_device")
        self.assertIn("hardware_configure", intent.capabilities)

    def test_primary_action_is_stable_across_device_and_task_variants(self):
        cases = [
            (
                "用 HackRF 构建发射机，中心频率 915 MHz，采样率 2 Msps",
                SharedState(),
                "TX_BUILD",
                {"build_tx", "hardware_configure"},
            ),
            (
                "配置 Pluto SDR，中心频率 868 MHz，采样率 1 Msps",
                SharedState(),
                "HARDWARE_CONFIGURE",
                {"hardware_configure"},
            ),
            (
                "诊断 USRP B210 当前为什么没有信号",
                project_state(),
                "DIAGNOSE",
                {"diagnose", "hardware_configure"},
            ),
            (
                "查看当前工程的频谱和星座图",
                project_state(),
                "OBSERVE",
                {"observe"},
            ),
        ]
        for text, state, task_type, capabilities in cases:
            with self.subTest(text=text):
                intent = self.engine().classify(text, state)
                self.assertEqual(intent.raw_text, text)
                self.assertEqual(intent.task_type, task_type)
                self.assertTrue(capabilities.issubset(intent.capabilities))

    def test_hardware_discovery_dispatch_is_device_family_driven(self):
        self.assertEqual(_device_command("b210", probe=False)[0], "uhd_find_devices")
        self.assertEqual(_device_command("hackrf", probe=True)[0], "hackrf_info")
        self.assertEqual(_device_command("pluto", probe=False)[0], "iio_info")
        self.assertEqual(_device_command("limesdr", probe=True)[0], "LimeUtil")
        pluto = resolve_hardware_profile("ADALM-Pluto")
        self.assertEqual(
            pluto.command(probe=True, identity="usb:1.2.3"),
            ["iio_info", "-u", "usb:1.2.3"],
        )
        generic_usrp = resolve_hardware_profile("USRP X310")
        self.assertEqual(generic_usrp.key, "usrp")
        self.assertEqual(generic_usrp.command(probe=False), ["uhd_find_devices"])
        self.assertNotEqual(generic_usrp.key, "b210")
        self.assertEqual(resolve_hardware_profile("unknown-sdr"), None)

    def test_generic_usrp_text_is_not_rewritten_as_b210(self):
        intent = self.engine().classify(
            "配置 USRP X310，中心频率 2.4 GHz，采样率 1 Msps",
            SharedState(),
        )
        self.assertEqual(intent.slots["hardware"], "usrp")
        self.assertNotEqual(intent.slots["hardware"], "b210")
        b210 = self.engine().classify(
            "配置 USRP B210，中心频率 2.4 GHz，采样率 1 Msps",
            SharedState(),
        )
        self.assertEqual(b210.slots["hardware"], "b210")

    def test_missing_hardware_parameter_is_a_global_gate(self):
        engine = self.engine()
        workflow = engine.consume_turn(self.hardware_rx_text(), SharedState())
        self.assertEqual(workflow.intent.missing_slots, [])
        self.assertEqual(workflow.intent.slots["sample_rate"], 2_000_000.0)
        self.assertEqual(workflow.intent.slot_sources["sample_rate"], "default")
        self.assertEqual(workflow.current_stage, "rx_build_and_verify")
        workflow = engine.consume_turn("采样率 4 Msps", SharedState())
        self.assertEqual(workflow.intent.missing_slots, [])
        self.assertEqual(workflow.intent.slots["sample_rate"], 4_000_000.0)
        self.assertEqual(workflow.current_stage, "rx_build_and_verify")

    def test_composed_build_transitions_to_hardware_not_completed(self):
        engine = self.engine()
        workflow = engine.consume_turn(
            self.hardware_rx_text(with_rate=True), SharedState()
        )
        stage = engine.start_stage()
        result = {
            "workflow_id": workflow.workflow_id,
            "stage_id": stage.id,
            "workflow_revision": workflow.revision,
            "base_project_version": workflow.base_project_version,
            "ok": True,
            "outcome": "passed",
            "completion": {name: True for name in stage.completion},
        }
        engine.accept_result(result)
        self.assertEqual(workflow.current_stage, "flowgraph_confirmation")
        self.assertNotEqual(workflow.execution_status, "completed")

    def test_realtime_capability_adds_bounded_runtime_safety_chain(self):
        workflow = self.engine().consume_turn(
            self.hardware_rx_text(with_rate=True), SharedState()
        )
        ids = [stage.id for stage in workflow.stages]
        self.assertEqual(ids, [
            "rx_build_and_verify",
            "flowgraph_confirmation",
        ])
        self.assertEqual(
            [item.get("id") for item in workflow.deferred_plan],
            [
            "hardware_precheck",
            "discover_and_probe_hardware",
            "rf_plan_confirmation",
            "configure_device",
            "run_bounded",
            "runtime_observation",
            "stop_runtime",
            ],
        )

    def test_configuration_only_request_does_not_start_runtime_chain(self):
        workflow = self.engine().consume_turn(
            "配置 Pluto SDR，中心频率 868 MHz，采样率 1 Msps",
            SharedState(),
        )
        ids = [stage.id for stage in workflow.stages]
        self.assertEqual(ids, [
            "hardware_precheck", "discover_and_probe_hardware",
            "hardware_confirmation",
        ])
        self.assertEqual(
            [item.get("id") for item in workflow.deferred_plan],
            ["configure_and_check"],
        )
        self.assertNotIn("hardware_runtime", workflow.intent.capabilities)

    def test_stop_at_tx_confirmation_is_deferred_not_forbidden(self):
        workflow = self.engine().consume_turn(
            "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，"
            "保存配置并停在发射确认。",
            SharedState(),
        )
        self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
        self.assertEqual(workflow.intent.slots["deploy_permission"], "pending")
        self.assertEqual(workflow.intent.slots.get("operation"), "prepare")
        self.assertIn(
            "stop_at_decision_boundary", workflow.intent.stop_conditions
        )
        self.assertNotIn("terminal_checkpoint", workflow.intent.slots)
        self.assertIn("hardware_runtime", workflow.intent.capabilities)
        ids = [stage.id for stage in workflow.stages]
        deferred = [item.get("id") for item in workflow.deferred_plan]
        self.assertEqual(ids, ["tx_build_and_validate", "flowgraph_confirmation"])
        self.assertIn("discover_and_probe_hardware", deferred)
        self.assertIn("rf_plan_confirmation", deferred)
        self.assertNotIn("configure_device", ids)
        self.assertNotIn("run_bounded", ids)
        self.assertNotIn("duration_seconds", workflow.intent.slots)
        self.assertNotIn("max_duration_seconds", workflow.intent.slots)
        self.assertLess(
            deferred.index("discover_and_probe_hardware"),
            deferred.index("rf_plan_confirmation"),
        )

    def test_do_not_transmit_remains_configuration_only(self):
        workflow = self.engine().consume_turn(
            "给 Pluto 配好发射流图，载频 915 MHz 采样率 2 Msps，先不要发射",
            SharedState(),
        )
        self.assertEqual(workflow.intent.slots["deploy_permission"], "forbidden")
        self.assertNotIn("hardware_runtime", workflow.intent.capabilities)
        self.assertNotIn(
            "rf_plan_confirmation", [stage.id for stage in workflow.stages]
        )

    def test_explicit_generic_deploy_uses_bounded_runtime_not_protocol_template(self):
        workflow = self.engine().consume_turn(
            "用 HackRF 构建发射机，中心频率 915 MHz，采样率 2 Msps，"
            "直接部署并运行 10 秒",
            SharedState(),
        )
        self.assertEqual(workflow.task_type, "TX_BUILD")
        self.assertEqual(workflow.intent.slots["duration_seconds"], 10.0)
        self.assertIn("hardware_runtime", workflow.intent.capabilities)
        self.assertIn(
            "run_bounded",
            [item.get("id") for item in workflow.deferred_plan],
        )
        self.assertNotIn("run_bounded", [stage.id for stage in workflow.stages])
        self.assertNotIn("build_ble_advertiser", [stage.id for stage in workflow.stages])

    def test_approval_materializes_deferred_rf_tail(self):
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            engine = self.engine()
            workflow = engine.consume_turn(
                "用 plutosdr 发射 ble 信号，local name 为 loveu",
                SharedState(),
            )
            self.assertNotIn("configure_device", [stage.id for stage in workflow.stages])
            engine.ensure_stage("rf_plan_confirmation")
            workflow.current_stage = "rf_plan_confirmation"
            engine._activate_current()
            digest = engine.digest()
            self.assertEqual(digest["wait_kind"], "approval")
            self.assertEqual(
                workflow.stage("rf_plan_confirmation").checkpoint.requested_effect,
                "RF_RUN",
            )
            self.assertEqual(
                workflow.stage("rf_plan_confirmation").checkpoint.purpose,
                "rf_authorization",
            )
            engine.resolve_checkpoint("approved")
            self.assertIn("transmit_bounded", [stage.id for stage in workflow.stages])
            self.assertEqual(workflow.current_stage, "configure_device")
            self.assertIn(
                "stop_and_finalize",
                [item.get("id") for item in workflow.deferred_plan],
            )

    def test_rf_grant_keeps_arm_start_when_llm_replans_away(self):
        """gui-f9463acf: LLM replaced the granted deploy tail after RF approval."""
        bad_proposal = [
            {"id": "discover_and_probe_hardware"},
            {"id": "build_ble_advertiser"},
            {"id": "hardware_precheck"},
            {"id": "tx_build_and_validate"},
            {"id": "rf_plan_confirmation"},
        ]
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            engine = self.engine()
            workflow = engine.consume_turn(
                "用 plutosdr 发射 ble 信号，local name 为 loveu",
                SharedState(),
            )
            engine.ensure_stage("rf_plan_confirmation")
            workflow.current_stage = "rf_plan_confirmation"
            engine._activate_current()
            with mock.patch(
                "grc.agent.workflow.llm_planner.propose_plan",
                return_value=bad_proposal,
            ):
                engine.resolve_checkpoint("approved")
        ids = [stage.id for stage in workflow.stages] + [
            str(item.get("id") or "") for item in workflow.deferred_plan
        ]
        self.assertEqual(workflow.current_stage, "configure_device")
        self.assertIn("configure_device", ids)
        self.assertIn("transmit_bounded", ids)
        self.assertIn("stop_and_finalize", ids)
        self.assertTrue(
            workflow.stage("rf_plan_confirmation").result["completion"]["rf_plan_approved"]
        )
        # The deploy template now uses the unified runtime-stage id (V6:
        # legacy ``discover_and_probe_device`` duplicate removed), so the
        # LLM's off-script additions — not the base chain — must be the ones
        # filtered out here.
        self.assertNotIn(
            "discover_and_probe_device",
            [stage.id for stage in workflow.stages],
        )
        self.assertLess(
            [stage.id for stage in workflow.stages].index("hardware_precheck"),
            [stage.id for stage in workflow.stages].index(
                "discover_and_probe_hardware"
            ),
        )

    def test_rf_grant_skips_llm_replan_when_only_stop_remains(self):
        """gui-cacf1e84: post-grant LLM replan blocked TX start for ~55s."""
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            engine = self.engine()
            with mock.patch(
                "grc.agent.workflow.llm_planner.propose_plan",
                return_value=None,
            ) as planner:
                workflow = engine.consume_turn(
                    "用 plutosdr 发射 ble 信号，local name 为 loveu",
                    SharedState(),
                )
                planner.reset_mock()
                engine.ensure_stage("rf_plan_confirmation")
                workflow.current_stage = "rf_plan_confirmation"
                engine._activate_current()
                engine.resolve_checkpoint("approved")
                planner.assert_not_called()
        self.assertEqual(workflow.current_stage, "configure_device")
        self.assertEqual(
            [item.get("id") for item in workflow.deferred_plan],
            ["stop_and_finalize"],
        )

    def test_stop_at_boundary_approval_does_not_materialize_rf(self):
        engine = self.engine()
        workflow = engine.consume_turn(
            "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，"
            "保存配置并停在发射确认。",
            SharedState(),
        )
        self.assertIn("stop_at_decision_boundary", workflow.intent.stop_conditions)
        engine.ensure_stage("rf_plan_confirmation")
        workflow.current_stage = "rf_plan_confirmation"
        engine._activate_current()
        digest = engine.digest()
        self.assertEqual(digest["wait_kind"], "approval")
        self.assertEqual(
            workflow.stage("rf_plan_confirmation").checkpoint.requested_effect,
            "DEVICE_READ",
        )
        self.assertEqual(
            workflow.stage("rf_plan_confirmation").checkpoint.purpose,
            "config_handoff",
        )
        self.assertEqual(digest.get("checkpoint_purpose"), "config_handoff")
        self.assertEqual(digest.get("stage_label"), "Configuration Confirmation")
        self.assertEqual(
            next(
                item["label"] for item in digest["stages"]
                if item["id"] == "rf_plan_confirmation"
            ),
            "Configuration Confirmation",
        )
        self.assertFalse(digest.get("max_duration_seconds"))
        self.assertEqual(workflow.stage("rf_plan_confirmation").checkpoint.reason, "Configuration Confirmation")
        self.assertFalse(digest.get("blocker"))
        engine.resolve_checkpoint("approved")
        self.assertEqual(workflow.execution_status, "completed")
        self.assertEqual(workflow.outcome, "passed")
        self.assertNotIn("configure_device", [stage.id for stage in workflow.stages])
        self.assertEqual(workflow.deferred_plan, [])
        digest = engine.digest()
        self.assertEqual(digest.get("wait_kind"), "")
        self.assertFalse(digest.get("can_confirm"))
        self.assertFalse(digest.get("checkpoint_id"))
        self.assertEqual(digest.get("requested_effect"), "")
        self.assertEqual(digest.get("stage_label"), "Configuration Confirmation")

    def test_followup_transmit_reuses_saved_preview_as_deploy(self):
        engine = self.engine()
        state = SharedState(session_id="followup-tx")
        preview = self.root / "pluto_tx.grc"
        preview.write_text("id: options\n", encoding="utf-8")
        workflow = engine.consume_turn(
            "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，"
            "保存配置并停在发射确认。",
            state,
        )
        engine.ensure_stage("rf_plan_confirmation")
        workflow.current_stage = "rf_plan_confirmation"
        engine._activate_current()
        engine.resolve_checkpoint("approved")
        self.assertEqual(workflow.execution_status, "completed")
        state.project.grc_path = str(preview)
        state.project.config.update({
            "hardware": "pluto",
            "direction": "tx",
            "carrier_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "preview_mode": "throttled_null_sink",
            "rf_armed": False,
        })
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            follow = engine.consume_turn("现在发射 10 秒", state)
            self.assertEqual(follow.intent.slots.get("operation"), "deploy")
            self.assertEqual(follow.intent.slots.get("hardware"), "pluto")
            self.assertEqual(follow.intent.slots.get("carrier_frequency"), 2_402_000_000.0)
            self.assertEqual(follow.intent.slots.get("sample_rate"), 2_000_000.0)
            self.assertEqual(follow.intent.slots.get("duration_seconds"), 10.0)
            self.assertEqual(follow.intent.slot_sources.get("hardware"), "current_project")
            self.assertNotIn("stop_at_decision_boundary", follow.intent.stop_conditions)
            self.assertIn("hardware_runtime", follow.intent.capabilities)
            engine.ensure_stage("rf_plan_confirmation")
            follow.current_stage = "rf_plan_confirmation"
            engine._activate_current()
            digest = engine.digest()
            self.assertEqual(
                follow.stage("rf_plan_confirmation").checkpoint.requested_effect,
                "RF_RUN",
            )
            self.assertEqual(digest.get("stage_label"), "RF Plan Confirmation")
            self.assertEqual(digest.get("max_duration_seconds"), 10.0)

    def test_failed_stage_keeps_resume_from(self):
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN", SharedState())
        stage = engine.start_stage()
        engine.accept_result({
            "workflow_id": workflow.workflow_id,
            "stage_id": stage.id,
            "workflow_revision": workflow.revision,
            "base_project_version": workflow.base_project_version,
            "ok": False,
            "outcome": "failed",
            "resume_from": "arm_flowgraph",
            "completion": {name: True for name in stage.completion},
        })
        self.assertEqual(stage.resume_from, "arm_flowgraph")
        self.assertEqual(workflow.execution_status, "waiting")

    def test_configure_device_resume_skips_already_applied_configure(self):
        from grc.agent.service.adapter import ServiceAgent
        from grc.agent.tools import registry
        from grc.agent import env

        try:
            platform = env.make_platform()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"make_platform unavailable: {exc}")
        sessions = self.root / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        with mock.patch(
            "grc.agent.service.session_store.sessions_root",
            return_value=str(sessions),
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id="resume-arm", platform=platform)
            engine = agent._workflow
            workflow = engine.consume_turn(
                "用 HackRF 构建发射机，中心频率 915 MHz，采样率 2 Msps，"
                "直接部署并运行 10 秒",
                agent._state,
            )
            engine.ensure_stage("configure_device")
            workflow.current_stage = "configure_device"
            workflow.intent.slots["direction"] = "tx"
            stage = workflow.stage("configure_device")
            stage.resume_from = "arm_flowgraph"
            stage.execution_status = "pending"
            workflow.execution_status = "pending"
            calls = []

            def fake_call(name, args=None, ctx=None):
                calls.append(name)
                if name == "configure_sdr":
                    return {"ok": True}
                if name == "arm_hardware_flowgraph":
                    return {"ok": False, "error": "arm failed"}
                return {"ok": True}

            ctx = agent._make_ctx()
            with mock.patch.object(registry, "call", side_effect=fake_call):
                agent._run_stage_deterministic(
                    ctx, "", "", True, "configure_device"
                )
            self.assertNotIn("configure_sdr", calls)
            self.assertIn("arm_hardware_flowgraph", calls)
            self.assertEqual(stage.resume_from, "arm_flowgraph")

    def test_open_compound_observe_is_not_a_seven_task_template(self):
        from grc.agent.tests.test_seven_tasks import VARIANTS

        text = (
            "在保持当前调制不变的前提下，只把频谱主峰写进报告，"
            "同时标出采样率，不要重新搭链路"
        )
        self.assertNotIn(text, {item[0] for item in VARIANTS})
        workflow = self.engine().consume_turn(text, project_state())
        ids = [stage.id for stage in workflow.stages]
        self.assertNotIn("configure_device", ids)
        self.assertNotIn("run_bounded", ids)
        self.assertNotIn("rf_plan_confirmation", ids)
        self.assertTrue(ids)
        self.assertNotIn(
            "hardware_runtime", workflow.intent.capabilities
        )

    def test_policy_checkpoint_resume_does_not_consume_an_attempt(self):
        engine = self.engine()
        engine.consume_turn("构建 BPSK AWGN", SharedState())
        stage = engine.start_stage()
        self.assertEqual(stage.attempt, 1)
        engine.wait_for_checkpoint("design_link", action="design_link")
        engine.resolve_checkpoint("approved")
        resumed = engine.start_stage()
        self.assertEqual(resumed.attempt, 1)
        self.assertEqual(resumed.execution_status, "running")

    def test_attempt_limit_is_enforced_at_start_boundary(self):
        engine = self.engine()
        engine.consume_turn("构建 BPSK AWGN", SharedState())
        stage = engine.current_stage()
        stage.attempt = stage.max_attempts
        stage.execution_status = "pending"
        blocked = engine.start_stage()
        self.assertEqual(blocked.execution_status, "waiting")
        self.assertEqual(engine.workflow.execution_status, "waiting")
        digest = engine.digest()
        self.assertEqual(
            digest["blocker"]["code"], "ATTEMPT_LIMIT_REACHED"
        )
        self.assertIn("attempt limit", digest["waiting_reason"])

    def test_device_range_validation_blocks_invalid_frequency(self):
        workflow = self.engine().consume_turn(
            "配置 USRP B210，中心频率 402 GHz，采样率 2 Msps",
            SharedState(),
        )
        self.assertIn(
            "carrier_frequency_out_of_device_range",
            workflow.intent.validation_errors,
        )
        self.assertEqual(workflow.current_stage, "spec_alignment")

    def test_hardware_completion_requires_matching_structure(self):
        path = self.root / "b210_rx.grc"
        path.write_text(
            "id: uhd_usrp_source\nvalue: 2402000000\nvalue: 2000000\n"
            "id: qtgui_freq_sink_x\n",
            encoding="utf-8",
        )
        state = SharedState(session_id="completion")
        state.project.grc_path = str(path)
        workflow = self.engine().consume_turn(
            self.hardware_rx_text(with_rate=True), state
        )
        stage = workflow.stage("rx_build_and_verify")
        # Build stages no longer require the endpoint predicate as a
        # completion (physical presence belongs to the discover stage, V6),
        # but the predicate itself must still judge flowgraph structure
        # correctly wherever it is used — so evaluate it explicitly.
        if "hardware_endpoint_present" not in stage.completion:
            stage.completion = list(stage.completion) + [
                "hardware_endpoint_present"
            ]
        reply = AgentReply(
            stage="FINAL",
            artifacts={"grc_path": str(path)},
            tool_invocations=[ToolInvocation(
                name="validate_flowgraph",
                result={"ok": True, "valid": True},
                ok=True,
            )],
        )
        result = evaluate(stage, workflow, state, reply)
        self.assertTrue(result["hardware_endpoint_present"])
        self.assertTrue(result["radio_parameters_match"])
        self.assertTrue(result["realtime_sink_present"])

        path.write_text(
            "id: analog_random_source_x\nid: channels_channel_model\n"
            "value: 2402000000\nvalue: 2000000\n",
            encoding="utf-8",
        )
        result = evaluate(stage, workflow, state, reply)
        self.assertFalse(result["hardware_endpoint_present"])
        self.assertFalse(result["realtime_sink_present"])


# --- test_intent_llm.py ---

import unittest
from unittest import mock

from grc.agent.state import SharedState
from grc.agent.workflow import WorkflowEngine
from grc.agent.workflow.engine import complete_intent
from grc.agent.workflow.schema import WorkflowIntent


class IntentLlmTest(unittest.TestCase):
    def test_unconfigured_keeps_rules_intent(self):
        rules = WorkflowIntent(
            raw_text="帮我做一个无线通信系统",
            task_type="END_TO_END_SIM",
            confidence=0.65,
            missing_slots=["modulation"],
        )
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertIs(completed, rules)

    def test_production_unconfigured_does_not_guess_from_rules(self):
        from grc.agent.llm import SemanticUnderstandingError

        rules = WorkflowIntent(
            raw_text="Build something with QPSK",
            task_type="END_TO_END_SIM",
            slots={"modulation": "qpsk", "direction": "sim"},
            slot_sources={"modulation": "rules", "direction": "rules"},
        )
        with mock.patch("grc.agent.llm.is_configured", return_value=False), mock.patch(
            "grc.agent.llm.intent_test_bypass_enabled", return_value=False
        ):
            with self.assertRaises(SemanticUnderstandingError):
                complete_intent(rules, rules.raw_text, SharedState())

    def test_llm_corrects_ambiguous_rule_candidate_to_tx(self):
        engine = WorkflowEngine(tempfile.mktemp(suffix=".json"))
        rules = engine.classify(
            "Build a QPSK baseband transmit chain, simulation only.", SharedState()
        )
        self.assertEqual(rules.slots["direction"], "sim")
        payload = {
            "task_type": "TX_BUILD",
            "confidence": 0.98,
            "capabilities": ["build_tx"],
            "slots": {"modulation": "qpsk", "direction": "tx"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots["direction"], "tx")
        self.assertEqual(completed.slot_sources["direction"], "llm")

    def test_v3_transmit_request_compiles_the_complete_rf_chain(self):
        text = "I want to use plutosdr to transmit a ble signal."
        rules = WorkflowIntent(
            raw_text=text,
            task_type="END_TO_END_SIM",
        )
        payload = {
            "task_type": "TX_BUILD",
            "execution_mode": "deploy",
            "confidence": 0.98,
            "capabilities": [
                "build_tx", "protocol", "hardware_configure",
            ],
            "slots": {
                "hardware": "plutosdr",
                "protocol": "ble",
                "direction": "tx",
            },
        }
        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            intent = complete_intent(rules, text, SharedState())
        self.assertEqual(intent.slots["operation"], "deploy")
        self.assertIn("hardware_runtime", intent.capabilities)
        self.assertIn("deploy", intent.capabilities)

        with tempfile.TemporaryDirectory() as root:
            engine = WorkflowEngine(str(Path(root) / "workflow.json"))
            workflow = engine.instantiate(intent, SharedState())
            ids = [node["stage_id"] for node in workflow.compiled_plan]
            self.assertEqual(
                ids,
                [
                    "build_ble_advertiser",
                    "offline_protocol_verify",
                    "flowgraph_confirmation",
                    "hardware_precheck",
                    "discover_and_probe_hardware",
                    "rf_plan_confirmation",
                    "configure_device",
                    "transmit_bounded",
                    "over_air_verification",
                    "stop_and_finalize",
                ],
            )

            visited = []
            with mock.patch.dict(
                os.environ, {"GRC_AGENT_ENABLE_RF": "1"}
            ), mock.patch(
                "grc.agent.workflow.llm_planner.propose_plan",
                return_value=None,
            ):
                while workflow.execution_status != "completed":
                    stage = engine.current_stage()
                    self.assertIsNotNone(stage)
                    visited.append(stage.id)
                    if "checkpoint" in stage.interaction:
                        engine.resolve_checkpoint("approved")
                        continue
                    running = engine.start_stage()
                    self.assertEqual(running.execution_status, "running")
                    engine.accept_result({
                        "ok": True,
                        "outcome": "passed",
                        "stage_id": stage.id,
                        "workflow_revision": workflow.revision,
                        "base_project_version": workflow.base_project_version,
                        "completion": {
                            name: True for name in stage.completion
                        },
                    })
            self.assertEqual(visited, ids)
            self.assertEqual(workflow.outcome, "passed")

    def test_production_intent_rejects_missing_execution_mode(self):
        rules = WorkflowIntent(raw_text="ambiguous", task_type="TX_BUILD")
        payload = {
            "task_type": "TX_BUILD",
            "confidence": 0.9,
            "capabilities": ["build_tx"],
            "slots": {"direction": "tx"},
        }
        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.intent_test_bypass_enabled", return_value=False
        ), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            with self.assertRaisesRegex(Exception, "semantic understanding"):
                complete_intent(rules, rules.raw_text, SharedState())

    def test_hardware_tx_design_ambiguity_gets_llm_effect_adjudication(self):
        text = "Use the attached radio to transmit the requested signal."
        initial = {
            "task_type": "TX_BUILD",
            "execution_mode": "design",
            "confidence": 0.9,
            "capabilities": ["build_tx", "protocol"],
            "slots": {
                "hardware": "generic_sdr",
                "protocol": "custom",
                "direction": "tx",
            },
        }
        adjudicated = {
            "execution_mode": "deploy",
            "reason": "The user requests actual transmission on hardware.",
        }
        with mock.patch(
            "grc.agent.llm.is_configured", return_value=True
        ), mock.patch(
            "grc.agent.llm.intent_test_bypass_enabled", return_value=False
        ), mock.patch(
            "grc.agent.llm.chat",
            side_effect=[json.dumps(initial), json.dumps(adjudicated)],
        ) as chat:
            intent = complete_intent(
                WorkflowIntent(raw_text=text, task_type="END_TO_END_SIM"),
                text,
                SharedState(),
            )
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(intent.context["execution_mode"], "deploy")
        self.assertEqual(intent.slots["operation"], "deploy")
        self.assertIn("hardware_runtime", intent.capabilities)

    def test_llm_extraction_is_authoritative_over_regex_user_guesses(self):
        """The LLM reading of the user's text is the user's voice.

        Regex layers may label extracted values "user", but they are still
        guesses about the text; the LLM extraction of the same text wins.
        (V4 regression: regex local_name="of" survived beside the LLM's
        corrected value, producing two disagreeing projections.)
        """
        rules = WorkflowIntent(
            raw_text="帮我做一个无线通信系统，用 BPSK",
            task_type="END_TO_END_SIM",
            confidence=0.65,
            slots={"modulation": "bpsk"},
            slot_sources={"modulation": "user"},
            missing_slots=[],
        )
        payload = {
            "task_type": "END_TO_END_SIM",
            "confidence": 0.92,
            "capabilities": ["build_signal"],
            "slots": {"modulation": "qpsk", "channel": "awgn"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots["modulation"], "qpsk")
        self.assertEqual(completed.slot_sources["modulation"], "llm")
        self.assertEqual(completed.slots["channel"], "awgn")
        self.assertEqual(completed.slot_sources["channel"], "llm")
        self.assertIn("build_signal", completed.capabilities)
        self.assertGreaterEqual(completed.confidence, 0.9)

    def test_llm_omission_drops_nonconflicting_rule_candidates(self):
        """A regex guess must not survive merely because the LLM omitted it."""
        rules = WorkflowIntent(
            raw_text="an ambiguous radio request",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.99,
            capabilities=["protocol", "deploy"],
            slots={"protocol": "ble", "local_name": "of"},
            slot_sources={"protocol": "rules", "local_name": "user"},
            goals=["rule-derived goal"],
            requested_operations=["deploy"],
        )
        payload = {
            "task_type": "END_TO_END_SIM",
            "confidence": 0.72,
            "capabilities": ["build_signal"],
            "slots": {"modulation": "qpsk"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        # 正则猜测(protocol=ble / local_name=of)必须被丢弃。
        self.assertNotIn("protocol", completed.slots)
        self.assertNotIn("local_name", completed.slots)
        self.assertEqual(completed.slots.get("modulation"), "qpsk")
        # direction 由 LLM 自己给出的 capabilities 派生(build_signal 且无硬件
        # => 基带仿真),不是从原文正则猜的,所以允许出现。
        self.assertEqual(completed.slots.get("direction"), "sim")
        self.assertEqual(completed.slot_sources.get("direction"), "derived")
        self.assertEqual(completed.capabilities, ["build_signal"])
        self.assertNotIn("rule-derived goal", completed.goals)
        self.assertNotIn("deploy", completed.requested_operations)
        self.assertAlmostEqual(completed.confidence, 0.72)

    def test_llm_turn_semantics_are_persisted_with_intent(self):
        rules = WorkflowIntent(raw_text="inspect only", task_type="DIAGNOSE")
        payload = {
            "task_type": "DIAGNOSE",
            "confidence": 0.9,
            "capabilities": ["diagnose"],
            "slots": {},
            "turn_semantics": {
                "read_only": True,
                "confirmation_decision": "none",
                "recipe_switch_target": "",
            },
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertTrue(completed.context["turn_semantics"]["read_only"])

    def test_llm_device_alias_merges_onto_hardware(self):
        """A `device` slot answer must land in canonical `hardware`.

        (V5 regression: the intent LLM answered with a `device` key; the
        spec then rendered duplicate Device rows and re-asked the user.)
        """
        rules = WorkflowIntent(
            raw_text="I want to use plutosdr to transmit ble signal.",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            capabilities=["build_tx", "protocol", "hardware_configure"],
            slots={"hardware": "pluto"},
            slot_sources={"hardware": "rules"},
        )
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.97,
            "capabilities": ["build_tx", "protocol", "hardware_configure"],
            "slots": {
                "device": "plutosdr",
                "protocol": "ble",
                "direction": "tx",
            },
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots.get("hardware"), "plutosdr")
        self.assertEqual(completed.slot_sources.get("hardware"), "llm")
        self.assertNotIn("device", completed.slots)

    def test_llm_omission_does_not_restore_rule_hardware(self):
        """A rule candidate never gets a second vote over the intent LLM."""
        rules = WorkflowIntent(
            raw_text="I want to use plutosdr to transmit ble signal.",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            capabilities=["build_tx", "protocol", "hardware_configure"],
            slots={"hardware": "pluto", "protocol": "ble"},
            slot_sources={"hardware": "rules", "protocol": "rules"},
        )
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.9,
            "capabilities": ["build_tx", "protocol", "hardware_configure"],
            "slots": {"direction": "tx"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertNotIn("hardware", completed.slots)
        self.assertNotIn("protocol", completed.slots)
        engine = WorkflowEngine(tempfile.mktemp(suffix=".json"))
        missing = engine._missing_slots(
            "HARDWARE_CONFIGURE", completed.slots, SharedState(),
            completed.capabilities,
        )
        self.assertIn("hardware", missing)

    def test_production_classify_has_no_rule_derived_semantics(self):
        engine = WorkflowEngine(tempfile.mktemp(suffix=".json"))
        state = SharedState(session_id="semantic-seed")
        state.project.grc_path = "/tmp/existing.grc"
        state.project.config.update({
            "hardware": "pluto", "carrier_frequency": 2_402_000_000.0,
        })
        with mock.patch(
            "grc.agent.llm.intent_test_bypass_enabled", return_value=False
        ):
            seed = engine.classify(
                "confirm to transmit a BLE signal with PlutoSDR", state
            )
        self.assertEqual(seed.slots, {})
        self.assertEqual(seed.capabilities, [])
        self.assertEqual(seed.task_type, "END_TO_END_SIM")
        self.assertEqual(
            seed.context["current_project"]["hardware"], "pluto"
        )

    def test_llm_answer_still_wins_over_seed_fallback(self):
        rules = WorkflowIntent(
            raw_text="use the b210 instead of plutosdr please",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            slots={"hardware": "pluto"},
            slot_sources={"hardware": "rules"},
        )
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.9,
            "capabilities": ["build_tx"],
            "slots": {"hardware": "b210"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots.get("hardware"), "b210")
        self.assertEqual(completed.slot_sources.get("hardware"), "llm")

    def test_llm_does_not_override_safety_bounds(self):
        rules = WorkflowIntent(
            raw_text="发射 10 秒",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            slots={"duration_seconds": 10.0, "max_duration_seconds": 10.0},
            slot_sources={
                "duration_seconds": "user",
                "max_duration_seconds": "safety_default",
            },
            missing_slots=[],
        )
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.9,
            "capabilities": ["deploy"],
            "slots": {"max_duration_seconds": 600.0},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots["max_duration_seconds"], 10.0)
        self.assertEqual(
            completed.slot_sources["max_duration_seconds"], "safety_default"
        )

    def test_llm_does_not_relabel_safety_duration_as_user_constraint(self):
        rules = WorkflowIntent(
            raw_text="给 Pluto 配发射流图并停在下一决策点",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            capabilities=["build_tx", "hardware_configure", "hardware_runtime"],
            slots={
                "operation": "prepare",
                "duration_seconds": 30.0,
                "hardware": "pluto",
            },
            slot_sources={
                "operation": "rules",
                "duration_seconds": "safety_default",
                "hardware": "user",
            },
        )
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.96,
            "capabilities": ["build_tx", "hardware_configure", "hardware_runtime"],
            "slots": {"duration_seconds": 30.0, "operation": "prepare"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slot_sources["duration_seconds"], "safety_default")
        self.assertEqual(completed.slots["duration_seconds"], 30.0)

    def test_llm_can_promote_prepare_to_deploy_from_user_goal(self):
        rules = WorkflowIntent(
            raw_text="用 Pluto 在 2.402 GHz 发射 10 秒",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            capabilities=["build_tx", "hardware_configure", "hardware_runtime"],
            slots={"operation": "prepare", "hardware": "pluto"},
            slot_sources={"operation": "rules", "hardware": "user"},
            stop_conditions=["stop_at_decision_boundary"],
        )
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.97,
            "capabilities": ["build_tx", "hardware_configure", "hardware_runtime"],
            "slots": {"operation": "deploy", "duration_seconds": 10.0},
            "stop_conditions": [],
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertEqual(completed.slots["operation"], "deploy")
        self.assertNotIn("stop_at_decision_boundary", completed.stop_conditions)

    def test_invalid_llm_contract_is_rejected_instead_of_guessed(self):
        from grc.agent.llm import SemanticUnderstandingError

        rules = WorkflowIntent(raw_text="x", task_type="RX_BUILD", confidence=0.5)
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value='{"task_type": "EIGHTH", "confidence": 0.99}'
        ):
            with self.assertRaises(SemanticUnderstandingError):
                complete_intent(rules, "x", SharedState())

    def test_llm_can_drop_hardware_capability(self):
        rules = WorkflowIntent(
            raw_text="搭 QPSK 发射链路，这次不要硬件",
            task_type="HARDWARE_CONFIGURE",
            confidence=0.95,
            capabilities=["build_tx", "hardware_configure"],
            slots={"modulation": "qpsk", "direction": "tx"},
            slot_sources={"modulation": "user", "direction": "user"},
        )
        payload = {
            "task_type": "TX_BUILD",
            "confidence": 0.96,
            "capabilities": ["build_tx"],
            "forbidden_capabilities": ["hardware_configure", "hardware_runtime"],
            "slots": {"modulation": "qpsk", "direction": "tx"},
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            completed = complete_intent(rules, rules.raw_text, SharedState())
        self.assertNotIn("hardware_configure", completed.capabilities)
        self.assertIn("build_tx", completed.capabilities)
        self.assertIn(
            "hardware_configure",
            completed.context.get("forbidden_capabilities") or [],
        )

    def test_ble_deploy_stays_hardware_when_llm_says_tx_build(self):
        engine = WorkflowEngine(tempfile.mktemp(suffix=".yaml"))
        payload = {
            "task_type": "HARDWARE_CONFIGURE",
            "confidence": 0.99,
            "capabilities": [
                "build_tx", "protocol", "hardware_configure", "deploy",
                "hardware_runtime",
            ],
            "slots": {
                "operation": "deploy", "protocol": "ble", "hardware": "pluto",
                "direction": "tx", "local_name": "demo",
            },
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            workflow = engine.consume_turn(
                "用 PlutoSDR 发射 BLE，local name 为 demo，直接部署",
                SharedState(),
            )
        self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
        self.assertGreater(len(workflow.stages), 0)
        self.assertEqual(workflow.stages[-1].id, "flowgraph_confirmation")
        self.assertEqual(
            [item.get("id") for item in workflow.deferred_plan][-1],
            "stop_and_finalize",
        )


_HW_STAGE_IDS = {
    "hardware_precheck",
    "hardware_confirmation",
    "configure_and_check",
    "discover_and_probe_hardware",
    "rf_plan_confirmation",
    "configure_device",
    "run_bounded",
    "runtime_observation",
    "stop_runtime",
    "discover_and_probe_device",
    "transmit_bounded",
    "over_air_verification",
    "stop_and_finalize",
}


class ConstraintCoverageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def _assert_sim_only_tx(self, text: str) -> None:
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            workflow = self.engine().consume_turn(text, SharedState())
        self.assertNotIn("hardware_configure", workflow.intent.capabilities)
        self.assertFalse({stage.id for stage in workflow.stages} & _HW_STAGE_IDS)
        self.assertIn(workflow.task_type, {"TX_BUILD", "END_TO_END_SIM"})

    def test_sim_only_tx_does_not_require_hardware(self):
        self._assert_sim_only_tx(
            "构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件。"
        )

    def test_open_negation_does_not_require_hardware(self):
        self._assert_sim_only_tx("先别接板子，只仿真，搭一个 QPSK 发射链路")

    def test_affirmed_hardware_still_opens_hardware_task(self):
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            workflow = self.engine().consume_turn(
                "配置 USRP B210，中心频率 2.4 GHz，采样率 1 MHz",
                SharedState(),
            )
        self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
        self.assertIn("hardware_configure", workflow.intent.capabilities)
        self.assertIn("hardware_precheck", {stage.id for stage in workflow.stages})

    def test_covering_recipe_none_for_novel_intent(self):
        from grc.agent.knowledge.recipes import covering_recipe

        self.assertIsNone(
            covering_recipe("做一个没有配方的空时编码架构", ["build_signal"])
        )
        covered = covering_recipe(
            "构建一个 QPSK 基带发射链路", ["build_tx"]
        )
        self.assertIsNotNone(covered)
        self.assertEqual(covered.name, "qpsk_tx")
        self.assertIsNone(
            covering_recipe(
                "为 Pluto 配置发射流图",
                ["build_tx", "hardware_configure"],
            )
        )

    def test_accept_result_errored_waits_instead_of_retry(self):
        engine = self.engine()
        workflow = engine.consume_turn("构建一个 QPSK 发射机", SharedState())
        stage = engine.start_stage()
        engine.accept_result(
            {
                "workflow_id": workflow.workflow_id,
                "stage_id": stage.id,
                "workflow_revision": workflow.revision,
                "base_project_version": workflow.base_project_version,
                "ok": False,
                "errored": True,
                "outcome": "failed",
                "completion": {name: False for name in stage.completion},
            }
        )
        self.assertEqual(engine.workflow.execution_status, "waiting")
        self.assertEqual(engine.current_stage().execution_status, "waiting")
        self.assertEqual(engine.current_stage().outcome, "inconclusive")
        self.assertEqual(engine.current_stage().id, stage.id)

    def test_timeout_recovers_existing_grc_without_second_invoke(self):
        from unittest import mock as _mock

        from grc.agent import env
        from grc.agent.service import adapter as adapter_mod
        from grc.agent.service import session_store as store
        from grc.agent.service.adapter import ServiceAgent

        try:
            platform = env.make_platform()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"make_platform unavailable: {exc}")
        sessions = self.root / "sessions"
        sessions.mkdir()
        invokes = {"n": 0}

        class FakeAgent:
            def invoke(self, *_args, **_kwargs):
                invokes["n"] += 1
                final = sessions / "timeout-grc" / "final"
                final.mkdir(parents=True, exist_ok=True)
                path = final / "partial.grc"
                path.write_text("<flow_graph/>", encoding="utf-8")
                raise TimeoutError("APITimeoutError")

        with _mock.patch.dict(
                 os.environ, {"DEEPRADIO_EXECUTION_MODE": "deepagents"}
             ), \
             _mock.patch.object(store, "sessions_root", return_value=str(sessions)), \
             _mock.patch(
                 "grc.agent.knowledge.recipes.covering_recipe", return_value=None
             ), \
             _mock.patch.object(
                 adapter_mod._orch, "build_agent", return_value=FakeAgent()
             ), \
             _mock.patch("grc.agent.llm.is_configured", return_value=False):
            agent = ServiceAgent(session_id="timeout-grc", platform=platform)
            reply = agent.step("构建一个 QPSK 空时编码链路")
        self.assertEqual(invokes["n"], 1)
        self.assertTrue(reply.artifacts.get("grc_path"))
        self.assertTrue(Path(reply.artifacts["grc_path"]).is_file())
        self.assertNotEqual(reply.workflow_digest.get("execution_status"), "running")
        self.assertNotEqual(reply.stage, "ERROR")


class Round2ContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def test_host_completion_does_not_override_invalid_llm_envelope(self):
        path = self.root / "radio.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="protocol-host")
        state.project.grc_path = str(path)
        state.project.flowgraph_version = 1
        engine = self.engine()
        workflow = engine.consume_turn(
            "诊断当前工程 EVM 偏高原因，先保持工程不变", state
        )
        stage = workflow.stage("inspect_and_diagnose")
        self.assertIsNotNone(stage)
        engine.workflow.current_stage = stage.id
        engine._activate_current()
        engine.start_stage()
        parent = make_task_card(workflow, stage, state, workflow.intent.raw_text)
        card = make_invocation_card(parent, "diagnosis_agent")
        bound = bind_invocation_result(
            vars(card),
            {
                "task_id": "mismatch",
                "outcome": "passed",
                "completion": {"diagnosis_created": True},
            },
            parent,
        )
        reply = AgentReply(
            text="诊断完成",
            stage="FINAL",
            artifacts={"metrics": {"evm_pct": 12.0}},
            tool_invocations=[
                ToolInvocation(
                    name="run_diagnosis_experiment",
                    args={},
                    result={
                        "ok": True,
                        "report_path": "/tmp/diagnosis_report.json",
                        "project_unchanged": True,
                    },
                    ok=True,
                ),
                ToolInvocation(
                    name="debug_by_metric",
                    args={},
                    result={"ok": True, "verdict": "噪声偏高"},
                    ok=True,
                )
            ],
        )
        envelope = make_result_envelope(
            workflow, stage, state, parent, reply, [bound]
        )
        self.assertFalse(envelope.ok)
        self.assertTrue(envelope.completion.get("diagnosis_created"))
        self.assertIn(
            "INVALID_EXECUTION_INVOCATION",
            envelope.acceptance.get("failure_codes") or [],
        )

    def test_readonly_diagnose_skips_repair_confirmation(self):
        state = SharedState(session_id="diag-ro")
        state.project.grc_path = "/tmp/current.grc"
        state.project.config.update({"recipe": "bpsk_awgn", "modulation": "bpsk"})
        with unittest.mock.patch(
            "grc.agent.llm.is_configured", return_value=False
        ):
            workflow = self.engine().consume_turn(
                "诊断当前链路的 EVM，解释主要原因并给出最小修改建议，先保持工程不变。",
                state,
            )
        self.assertEqual(workflow.task_type, "DIAGNOSE")
        self.assertIn("modify_project", workflow.intent.context.get("forbidden_capabilities") or [])
        ids = {stage.id for stage in workflow.stages}
        self.assertIn("inspect_and_diagnose", ids)
        self.assertNotIn("repair_confirmation", ids)
        inspect = workflow.stage("inspect_and_diagnose")
        self.assertEqual(inspect.transitions.get("failed"), "completed")

    def test_inspect_and_plan_failure_still_reaches_confirm(self):
        state = SharedState(session_id="mod-plan")
        state.project.grc_path = "/tmp/current.grc"
        state.project.config.update({"recipe": "bpsk_awgn", "modulation": "bpsk"})
        engine = self.engine()
        workflow = engine.consume_turn("把当前 BPSK 改成 QPSK", state)
        stage = workflow.stage("inspect_and_plan")
        self.assertEqual(stage.transitions.get("failed"), "change_confirmation")
        engine.start_stage()
        engine.accept_result(
            {
                "workflow_id": workflow.workflow_id,
                "stage_id": stage.id,
                "workflow_revision": workflow.revision,
                "base_project_version": workflow.base_project_version,
                "ok": False,
                "outcome": "failed",
                "completion": {name: False for name in stage.completion},
            }
        )
        self.assertEqual(engine.workflow.current_stage, "change_confirmation")
        self.assertEqual(state.project.config.get("modulation"), "bpsk")
        self.assertEqual(state.project.config.get("recipe"), "bpsk_awgn")

    def test_observe_uses_canvas_bound_project(self):
        path = self.root / "opened.grc"
        path.write_text("id: analog_sig_source_x\n", encoding="utf-8")
        state = SharedState(session_id="observe-canvas")
        state.project.grc_path = str(path)
        state.project.flowgraph_version = 1
        workflow = self.engine().consume_turn("查看当前工程频谱和星座图", state)
        self.assertEqual(workflow.task_type, "OBSERVE")
        self.assertNotIn("current_project", workflow.intent.missing_slots)

    def test_hardware_configure_not_stolen_by_tx_build(self):
        cases = (
            "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认。",
            "给 Pluto 配好发射流图，载频 915 MHz 采样率 2 Msps，先不要发射",
        )
        for text in cases:
            with self.subTest(text=text):
                with unittest.mock.patch(
                    "grc.agent.llm.is_configured", return_value=False
                ):
                    workflow = self.engine().consume_turn(text, SharedState())
                self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
                self.assertNotEqual(workflow.current_stage, "spec_alignment")
                self.assertNotIn("modulation", workflow.intent.missing_slots)

    def test_receive_quality_rejects_random_ber(self):
        from grc.agent.workflow.completion import evaluate

        path = self.root / "rx.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="ber-gate")
        state.project.grc_path = str(path)
        workflow = self.engine().consume_turn(
            "构建 BPSK 接收机并测 BER，Eb/N0 8 dB", state
        )
        stage = workflow.stage("rx_build_and_verify")
        reply = AgentReply(
            stage="FINAL",
            artifacts={"grc_path": str(path), "metrics": {"ber": 0.475}},
        )
        result = evaluate(stage, workflow, state, reply)
        self.assertFalse(result["receive_quality_evaluated"])
        report = {
            "valid": True,
            "value": 0.02,
            "bit_errors": 20,
            "compared_bits": 1000,
            "alignment_method": "bounded_delay_search",
            "tx_probe": "tx_sink",
            "rx_probe": "sink",
        }
        reply.artifacts["metrics"] = {"ber": 0.02, "ber_report": report}
        reply.tool_invocations = [ToolInvocation(
            name="run_simulation",
            args={},
            result={"ok": True, "probes": {
                "tx_sink": "tx.bin", "sink": "rx.bin",
            }},
            ok=True,
        )]
        state.claims.append(Claim(
            id="ber_measured",
            statement="BER measured from bound TX/RX probes",
            layer="sim",
            status="Passed",
            project_version=state.project.flowgraph_version,
            evidence=[Evidence(
                test="ber_report",
                observation=report,
                project_version=state.project.flowgraph_version,
            )],
        ))
        result = evaluate(stage, workflow, state, reply)
        self.assertTrue(result["receive_quality_evaluated"])

    def test_receive_quality_rejects_unverifiable_low_ber(self):
        from grc.agent.workflow.completion import evaluate

        path = self.root / "rx-unverified.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="ber-unverified")
        state.project.grc_path = str(path)
        workflow = self.engine().consume_turn(
            "构建 BPSK 接收机并测 BER，Eb/N0 8 dB", state
        )
        stage = workflow.stage("rx_build_and_verify")
        reply = AgentReply(
            stage="FINAL",
            artifacts={"grc_path": str(path), "metrics": {"ber": 0.0}},
        )
        self.assertFalse(
            evaluate(stage, workflow, state, reply)["receive_quality_evaluated"]
        )


class MutationAndExportContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def test_tool_payload_succeeded_treats_deny_as_failure(self):
        from grc.agent.workflow.completion import tool_payload_succeeded

        self.assertFalse(tool_payload_succeeded({"ok": False}))
        self.assertFalse(tool_payload_succeeded({
            "policy": "DENY", "error": "本轮禁止改图",
        }))
        self.assertFalse(tool_payload_succeeded({"error": "failed"}))
        self.assertTrue(tool_payload_succeeded({"ok": True, "valid": True}))

    def test_denied_design_link_does_not_satisfy_flowgraph_saved(self):
        from grc.agent.workflow.completion import evaluate

        path = self.root / "radio.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="deny-saved")
        state.project.grc_path = str(path)
        workflow = self.engine().consume_turn("构建 BPSK AWGN 并测 EVM", state)
        stage = workflow.stage("build_and_verify")
        reply = AgentReply(
            text="本轮禁止改图",
            stage="DENY",
            artifacts={"grc_path": str(path)},
            tool_invocations=[
                ToolInvocation(
                    name="design_link",
                    args={},
                    result={
                        "ok": False,
                        "policy": "DENY",
                        "error": "本轮禁止改图",
                    },
                    ok=True,
                )
            ],
        )
        result = evaluate(stage, workflow, state, reply)
        self.assertFalse(result["flowgraph_saved"])

    def test_apply_without_mutating_success_is_not_saved(self):
        from grc.agent.workflow.completion import evaluate

        path = self.root / "radio.grc"
        path.write_text("<flow_graph/>", encoding="utf-8")
        state = SharedState(session_id="apply-stale")
        state.project.grc_path = str(path)
        state.project.config.update({"recipe": "bpsk_awgn", "modulation": "bpsk"})
        engine = self.engine()
        workflow = engine.consume_turn("把当前 BPSK 改成 QPSK", state)
        stage = engine.ensure_stage("apply_and_verify")
        reply = AgentReply(
            text="完成",
            stage="FINAL",
            artifacts={"grc_path": str(path)},
            tool_invocations=[
                ToolInvocation(
                    name="validate_flowgraph",
                    args={},
                    result={"ok": True, "valid": True},
                    ok=True,
                )
            ],
        )
        result = evaluate(stage, workflow, state, reply)
        self.assertFalse(result["flowgraph_saved"])

    def test_digest_wait_kind_denied_for_mutation_refuse(self):
        state = SharedState(session_id="deny-wait")
        engine = self.engine()
        workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", state)
        stage = engine.start_stage()
        engine.accept_result(
            {
                "workflow_id": workflow.workflow_id,
                "stage_id": stage.id,
                "workflow_revision": workflow.revision,
                "base_project_version": workflow.base_project_version,
                "ok": False,
                "outcome": "failed",
                "note": "本轮禁止改图（用户要求只诊断/先不要修改）",
                "completion": {name: False for name in stage.completion},
                "acceptance": {"failure_codes": ["REPLY_STATUS_REJECTED"]},
            }
        )
        digest = engine.digest()
        self.assertEqual(digest["wait_kind"], "denied")
        self.assertIn("禁止改图", digest["waiting_reason"])

    def test_export_manifest_dedupes_paths(self):
        from grc.agent.service import session_store as store

        dest = self.root / "export"
        dest.mkdir()
        artifact = dest / "bpsk_awgn.grc"
        artifact.write_text("id: radio\n", encoding="utf-8")
        path = store.write_export_manifest(
            "dup-export",
            str(dest),
            [str(artifact), str(artifact)],
        )
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        paths = [item["path"] for item in payload["artifacts"]]
        self.assertEqual(paths, ["bpsk_awgn.grc"])

    def test_commit_intent_ignores_taskcard_dump(self):
        from grc.agent.tools.registry import ToolContext
        from grc.agent.tools.state_tools import commit_intent

        state = SharedState(session_id="dump")
        ctx = ToolContext(extra={"state": state})
        dump = "TaskCard USER DECISIONS\n" + ("x" * 80)
        result = commit_intent(ctx, dump)
        self.assertFalse(result["ok"])
        self.assertEqual(state.spec.goals, [])
        state.intent = SharedIntent.new("把当前 BPSK 改成 QPSK")
        state.intent.status = "confirmed"
        state.intent.goals = ["把当前 BPSK 改成 QPSK"]
        state.intent.parameters = {"modulation": "qpsk"}
        state.intent.parameter_sources = {"modulation": "llm"}
        committed = commit_intent(ctx, "this raw text must not be interpreted")
        self.assertTrue(committed["ok"])
        self.assertEqual(state.spec.goals, ["把当前 BPSK 改成 QPSK"])
        self.assertEqual(state.spec.decisions[0].value, "qpsk")


class RecipeIndexAndSpecHygieneTest(unittest.TestCase):
    def test_committed_recipe_index_matches_recipes(self):
        from grc.agent.knowledge.recipes import (
            RECIPE_INDEX_PATH,
            render_recipe_index,
        )

        self.assertTrue(RECIPE_INDEX_PATH.is_file())
        self.assertEqual(
            RECIPE_INDEX_PATH.read_text(encoding="utf-8"),
            render_recipe_index(),
        )

    def test_spec_clarify_does_not_write_goals(self):
        from grc.agent.tools.registry import ToolContext
        from grc.agent.tools.state_tools import spec_clarify

        state = SharedState(session_id="clarify")
        ctx = ToolContext(extra={"state": state})
        result = spec_clarify(
            ctx,
            "诊断当前链路的 EVM，解释主要原因，先保持工程不变。",
        )
        self.assertEqual(state.spec.goals, [])
        self.assertEqual(state.spec.decisions, [])
        self.assertFalse(result["complete"])
        self.assertIn("LLM intent alignment", result["open_questions"][0])

        state.intent = SharedIntent.new("诊断当前链路")
        state.intent.status = "awaiting_input"
        state.intent.missing_fields = ["modulation"]
        waiting = spec_clarify(ctx, "ignored")
        self.assertEqual(waiting["open_questions"], ["modulation"])
        state.intent.status = "confirmed"
        state.intent.missing_fields = []
        filled = spec_clarify(ctx, "诊断当前链路的 EVM，先保持工程不变。")
        self.assertEqual(state.spec.goals, [])
        self.assertTrue(filled["complete"])
        self.assertEqual(filled["open_questions"], [])


class IntentAlignmentContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = SharedState(session_id="intent-contract")
        self.engine = WorkflowEngine(
            str(Path(self.temp.name) / "workflow.json")
        )
        self.alignment = IntentAlignmentCoordinator(self.engine, self.state)

    def tearDown(self):
        self.temp.cleanup()

    def respond(self, **payload):
        interaction = self.state.intent.interaction
        return self.alignment.consume_response({
            "interaction_id": interaction["interaction_id"],
            "base_intent_revision": interaction["base_intent_revision"],
            **payload,
        })

    def test_vague_ble_request_aligns_then_confirms(self):
        first = self.alignment.consume_text("我要用硬件发射一段ble信号")
        self.assertTrue(first.pending)
        self.assertEqual(self.state.intent.interaction["field"], "hardware")
        self.assertEqual(
            self.state.intent.interaction["fields"],
            ["hardware", "duration_seconds", "success_conditions"],
        )
        self.alignment.consume_text(
            "硬件用 PlutoSDR，最多20秒，成功条件是独立接收端观察到目标信号"
        )
        self.assertEqual(
            self.state.intent.interaction["kind"], "intent_confirmation"
        )
        final = self.respond(decision="approved")
        self.assertFalse(final.pending)
        self.assertEqual(self.state.intent.status, "confirmed")
        self.assertEqual(final.intent.slots["hardware"], "pluto")
        self.assertNotIn("local_name", final.intent.slots)
        self.assertEqual(final.intent.slots["direction"], "tx")
        self.assertEqual(final.intent.slots["max_duration_seconds"], 20.0)
        self.assertEqual(
            final.intent.slots["success_conditions"],
            ["独立接收端观察到目标信号"],
        )
        self.assertTrue(self.state.intent.semantic_hash)

    def test_llm_followup_absorbs_all_answered_and_optional_fields(self):
        self.alignment.consume_text(
            "I want to use PlutoSDR to transmit a BLE signal."
        )
        payload = {
            "updates": {
                "duration_seconds": 30,
                "success_conditions": [
                    "The cindysha advertisement is visible in the phone app"
                ],
                "local_name": "cindysha",
            }
        }
        config = {
            "base_url": "https://unused.invalid",
            "api_key": "test",
            "model": "test-model",
            "timeout": 120,
            "max_messages": 20,
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.get_config", return_value=config
        ), mock.patch(
            "grc.agent.llm.chat", return_value=__import__("json").dumps(payload)
        ):
            result = self.alignment.consume_text(
                "30s; use local name 'cindysha' so I can observe it in my phone app."
            )
        self.assertTrue(result.pending)
        self.assertEqual(self.state.intent.interaction["kind"], "intent_confirmation")
        self.assertEqual(self.state.intent.parameters["duration_seconds"], 30.0)
        self.assertEqual(self.state.intent.parameters["max_duration_seconds"], 30.0)
        self.assertEqual(self.state.intent.parameters["local_name"], "cindysha")
        self.assertEqual(self.state.intent.missing_fields, [])

    def test_llm_followup_timeout_preserves_specification(self):
        from grc.agent.llm import SemanticUnderstandingError

        self.alignment.consume_text(
            "I want to use PlutoSDR to transmit a BLE signal."
        )
        before = dict(self.state.intent.parameters)
        config = {
            "base_url": "https://unused.invalid",
            "api_key": "test",
            "model": "test-model",
            "timeout": 120,
            "max_messages": 20,
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.get_config", return_value=config
        ), mock.patch(
            "grc.agent.llm.chat", side_effect=TimeoutError("timed out")
        ), mock.patch.dict(os.environ, {"GRC_AGENT_SEMANTIC_RETRIES": "0"}):
            with self.assertRaises(SemanticUnderstandingError):
                self.alignment.consume_text("30 seconds, verify it in my phone app")
        self.assertEqual(self.state.intent.parameters, before)

    def test_confirmation_action_uses_llm_semantics_not_fixed_vocabulary(self):
        self.alignment.consume_text("我要用硬件发射一段ble信号")
        self.alignment.consume_text(
            "硬件用 PlutoSDR，最多20秒，成功条件是独立接收端观察到目标信号"
        )
        self.assertEqual(self.state.intent.status, "awaiting_confirmation")
        payload = {"intent_action": "confirm", "updates": {}}
        config = {
            "base_url": "https://unused.invalid", "api_key": "test",
            "model": "test-model", "timeout": 120, "max_messages": 20,
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.get_config", return_value=config
        ), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            outcome = self.alignment.consume_text(
                "Everything in that specification matches what I meant."
            )
        self.assertFalse(outcome.pending)
        self.assertEqual(self.state.intent.status, "confirmed")

    def test_confirmation_while_field_pending_accepts_sole_choice(self):
        """A confirm reply during awaiting_input must not re-ask forever.

        Session gui-2c67ce08: a DIAGNOSE intent waited on current_project
        and three consecutive alignment turns returned intent_action=confirm
        with empty updates; the turn was dropped and the same question
        looped indefinitely.
        """
        self.alignment.consume_text("诊断当前链路的 EVM，先不要修改")
        shared = self.state.intent
        self.assertEqual(shared.status, "awaiting_input")
        self.assertIn("current_project", shared.missing_fields)
        payload = {"intent_action": "confirm", "updates": {}}
        config = {
            "base_url": "https://unused.invalid", "api_key": "test",
            "model": "test-model", "timeout": 120, "max_messages": 20,
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.get_config", return_value=config
        ), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ):
            outcome = self.alignment.consume_text("confirm")
        self.assertTrue(outcome.pending)
        self.assertEqual(shared.parameters.get("current_project"), "current_canvas")
        self.assertNotEqual(shared.interaction.get("field"), "current_project")
        self.assertNotEqual(
            shared.status,
            "awaiting_input",
            "the sole-choice field must resolve instead of re-asking",
        )

    def test_orphan_confirmation_preserves_existing_shared_intent(self):
        """A control turn cannot become a new underspecified radio request."""
        existing = SharedIntent.new("Transmit the configured BLE signal")
        existing.status = "confirmed"
        existing.parameters = {
            "hardware": "pluto", "protocol": "ble", "direction": "tx",
            "duration_seconds": 30.0,
        }
        self.state.intent = existing
        semantic = WorkflowIntent(
            raw_text="confirm to transmit",
            task_type="HARDWARE_CONFIGURE",
            capabilities=["deploy", "hardware_runtime"],
            context={"turn_semantics": {
                "confirmation_decision": "approved", "read_only": False,
                "recipe_switch_target": "",
            }},
        )
        with mock.patch.object(
            self.engine, "classify", return_value=semantic
        ), mock.patch.object(
            self.engine, "_reconcile_intent", return_value=semantic
        ):
            outcome = self.alignment.consume_text("confirm to transmit")
        self.assertTrue(outcome.pending)
        self.assertIn("no pending confirmation", outcome.message.lower())
        self.assertIs(self.state.intent, existing)
        self.assertEqual(self.state.intent.parameters["hardware"], "pluto")

    def test_confirmation_parameter_update_is_extracted_in_same_llm_call(self):
        self.alignment.consume_text("我要用硬件发射一段ble信号")
        self.alignment.consume_text(
            "硬件用 PlutoSDR，最多20秒，成功条件是独立接收端观察到目标信号"
        )
        payload = {
            "intent_action": "param_update",
            "updates": {"local_name": "research-demo"},
        }
        config = {
            "base_url": "https://unused.invalid", "api_key": "test",
            "model": "test-model", "timeout": 120, "max_messages": 20,
        }
        with mock.patch("grc.agent.llm.is_configured", return_value=True), mock.patch(
            "grc.agent.llm.get_config", return_value=config
        ), mock.patch(
            "grc.agent.llm.chat", return_value=json.dumps(payload)
        ) as chat:
            outcome = self.alignment.consume_text(
                "One adjustment: call the advertisement research-demo."
            )
        self.assertTrue(outcome.pending)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(
            self.state.intent.parameters["local_name"], "research-demo"
        )

    def test_radio_specification_table_fills_open_fields_atomically(self):
        first = self.alignment.consume_text("我要用硬件发射一段ble信号")
        interaction = dict(self.state.intent.interaction)
        outcome = self.alignment.consume_updates({
            "intent_id": self.state.intent.intent_id,
            "interaction_id": interaction["interaction_id"],
            "base_intent_revision": interaction["base_intent_revision"],
            "updates": {
                "hardware": "pluto",
                "local_name": "table-demo",
                "advertising_channels": [37],
                "duration_seconds": 30.0,
                "success_conditions": "独立接收端观察到 table-demo",
            },
        })
        self.assertTrue(first.pending)
        self.assertTrue(outcome.pending)
        self.assertEqual(
            self.state.intent.interaction["kind"], "intent_confirmation"
        )
        self.assertEqual(self.state.intent.missing_fields, [])
        self.assertEqual(
            self.state.intent.parameters["success_conditions"],
            ["独立接收端观察到 table-demo"],
        )
        self.assertEqual(
            self.state.intent.parameter_sources["duration_seconds"], "user_choice"
        )

    def test_radio_specification_is_read_only_and_exposes_suggestions(self):
        self.alignment.consume_text("我要用硬件发射一段ble信号")
        digest = self.state.spec_digest()
        rows = {item["key"]: item for item in digest["radio_specification"]}
        duration = rows["duration_seconds"]
        self.assertEqual(duration["display_value"], "30 s")
        self.assertEqual(duration["source"], "safety_default")
        self.assertTrue(duration["needs_confirmation"])
        self.assertTrue(duration["choices"])
        self.assertFalse(rows["modulation"]["editable"])
        self.assertTrue(rows["modulation"]["locked"])
        self.assertEqual(rows["modulation"]["display_value"], "GFSK")

    def test_optional_ble_name_only_appears_when_user_mentions_it(self):
        self.alignment.consume_text("我要用硬件发射一段 BLE 信号")
        unnamed = {
            item["key"] for item in self.state.spec_digest()["radio_specification"]
        }
        self.assertNotIn("local_name", unnamed)

        other_state = SharedState(session_id="named-intent")
        other = IntentAlignmentCoordinator(self.engine, other_state)
        other.consume_text(
            "我要用硬件发射 BLE，local name 为 research-demo"
        )
        named_rows = {
            item["key"]: item for item in other_state.spec_digest()["radio_specification"]
        }
        self.assertEqual(named_rows["local_name"]["requirement"], "mentioned")
        self.assertEqual(named_rows["local_name"]["value"], "research-demo")

    def test_partial_natural_reply_only_reasks_unanswered_required_fields(self):
        self.alignment.consume_text("我要用硬件发射一段 BLE 信号")
        outcome = self.alignment.consume_text("硬件用 PlutoSDR")
        self.assertEqual(
            self.state.intent.missing_fields,
            ["duration_seconds", "success_conditions"],
        )
        self.assertEqual(
            self.state.intent.interaction["fields"],
            ["duration_seconds", "success_conditions"],
        )
        self.assertNotIn("Proposed radio specification", outcome.message)

    def test_confirmation_reply_does_not_repeat_specification_card(self):
        self.alignment.consume_text("我要用硬件发射一段 BLE 信号")
        outcome = self.alignment.consume_text(
            "硬件用 PlutoSDR，最多20秒，成功条件是独立接收端观察到目标信号"
        )
        self.assertEqual(self.state.intent.status, "awaiting_confirmation")
        self.assertIn("Specification is complete", outcome.message)
        self.assertNotIn("Proposed radio specification", outcome.message)
        self.assertNotIn("PlutoSDR", outcome.message)

    def test_optional_catalog_and_teaching_do_not_confirm_or_mutate_values(self):
        self.alignment.consume_text("我要用硬件发射一段 BLE 信号")
        self.alignment.consume_text(
            "硬件用 PlutoSDR，最多20秒，成功条件是独立接收端观察到目标信号"
        )
        revision = self.state.intent.revision
        parameters = dict(self.state.intent.parameters)
        catalog = self.alignment.consume_text("有哪些可选字段")
        self.assertIn("Advertising name", catalog.message)
        teaching = self.alignment.consume_text("介绍这些参数")
        self.assertIn("This explanation does not confirm", teaching.message)
        self.assertEqual(self.state.intent.status, "awaiting_confirmation")
        self.assertEqual(self.state.intent.revision, revision)
        self.assertEqual(self.state.intent.parameters, parameters)

        self.alignment.consume_text("local name 改为 research-demo")
        self.assertEqual(
            self.state.intent.parameters["local_name"], "research-demo"
        )
        added = next(
            item for item in self.state.intent.specification.fields
            if item.key == "local_name"
        )
        self.assertEqual(added.requirement, "optional_added")
        self.assertEqual(self.state.intent.status, "awaiting_confirmation")

    def test_radio_specification_export_matches_canonical_intent_revision(self):
        self.alignment.consume_text("我要用硬件发射一段 BLE 信号")
        state_path = Path(self.temp.name) / "state.json"
        self.state.save(str(state_path))
        exported = json.loads(
            (Path(self.temp.name) / "radio_specification.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(exported["intent_id"], self.state.intent.intent_id)
        self.assertEqual(exported["revision"], self.state.intent.revision)
        self.assertEqual(exported["semantic_hash"], self.state.intent.semantic_hash)
        self.assertEqual(
            exported["specification"], self.state.intent.specification.to_dict()
        )

    def test_specification_merges_device_alias_into_single_hardware_row(self):
        """`device` + `hardware` slots must render as one Device field.

        (V5 regression: the spec card showed two Device rows — one required
        from user_text, one mentioned from the LLM's `device` key.)
        """
        from grc.agent.knowledge.spec_requirements import resolve_specification

        spec = resolve_specification(
            task_type="HARDWARE_CONFIGURE",
            capabilities=["build_tx", "protocol", "hardware_configure"],
            slots={
                "hardware": "plutosdr",
                "device": "plutosdr",
                "protocol": "ble",
                "direction": "tx",
            },
            slot_sources={
                "hardware": "user_text",
                "device": "llm",
                "protocol": "llm",
                "direction": "llm",
            },
            missing_fields=[],
            validation_errors=[],
        )
        field_keys = [field.key for field in spec.fields]
        device_rows = [key for key in field_keys if key in ("device", "hardware")]
        self.assertEqual(device_rows, ["hardware"])
        hardware_field = next(
            field for field in spec.fields if field.key == "hardware"
        )
        self.assertEqual(hardware_field.value, "plutosdr")

    def test_modulation_and_direction_are_required_spec_fields(self):
        from grc.agent.knowledge.spec_requirements import resolve_specification

        spec = resolve_specification(
            task_type="HARDWARE_CONFIGURE",
            capabilities=["build_tx", "protocol", "deploy", "hardware_runtime"],
            slots={
                "hardware": "pluto",
                "protocol": "ble",
                "direction": "tx",
                "modulation": "gfsk",
                "operation": "deploy",
            },
            slot_sources={
                "hardware": "llm",
                "protocol": "llm",
                "direction": "llm",
                "modulation": "protocol_default",
                "operation": "llm",
            },
            missing_fields=[],
            validation_errors=[],
        )
        rows = {field.key: field for field in spec.fields}
        self.assertEqual(rows["modulation"].requirement, "required")
        self.assertEqual(rows["direction"].requirement, "required")
        self.assertEqual(rows["direction"].value, "tx")

    def test_direction_transceiver_alias_normalizes_to_sim(self):
        from grc.agent.knowledge.spec_requirements import resolve_specification

        spec = resolve_specification(
            task_type="TX_BUILD",
            capabilities=["build_tx"],
            slots={"modulation": "qpsk", "direction": "transceiver"},
            slot_sources={"modulation": "llm", "direction": "llm"},
            missing_fields=[],
            validation_errors=[],
        )
        rows = {field.key: field for field in spec.fields}
        self.assertEqual(rows["direction"].value, "sim")
        self.assertEqual(rows["direction"].requirement, "required")

    def test_protocol_profiles_are_composed_without_task_sentence_branches(self):
        from grc.agent.knowledge.spec_requirements import resolve_specification

        common = {
            "task_type": "HARDWARE_CONFIGURE",
            "capabilities": ["protocol", "hardware_runtime", "deploy"],
            "slot_sources": {
                "hardware": "user", "direction": "user",
                "protocol": "user", "duration_seconds": "user",
                "success_conditions": "user",
            },
            "missing_fields": [], "validation_errors": [],
        }
        ble = resolve_specification(
            **common,
            slots={
                "hardware": "pluto", "direction": "tx", "protocol": "ble",
                "operation": "deploy",
                "duration_seconds": 10, "success_conditions": ["observe"],
            },
        )
        wifi = resolve_specification(
            **common,
            slots={
                "hardware": "b210", "direction": "tx", "protocol": "wifi",
                "operation": "deploy",
                "wifi_role": "beacon", "duration_seconds": 10,
                "success_conditions": ["observe"],
            },
        )
        generic = resolve_specification(
            task_type="END_TO_END_SIM", capabilities=["build_signal"],
            slots={"modulation": "bpsk"}, slot_sources={"modulation": "user"},
            missing_fields=[], validation_errors=[],
        )
        self.assertIn("protocol_ble", ble.profile_refs)
        self.assertIn("protocol_wifi", wifi.profile_refs)
        self.assertNotIn("protocol_ble", generic.profile_refs)
        self.assertIn("local_name", [item["field"] for item in ble.optional_prompts])
        self.assertIn("ssid", [item["field"] for item in wifi.optional_prompts])
        self.assertNotIn("ssid", [item["field"] for item in generic.optional_prompts])
        self.assertTrue(all(not item.reason for item in ble.fields))
        self.assertTrue(all(not item.reason for item in wifi.fields))
        self.assertTrue(all(not item.reason for item in generic.fields))

    def test_physical_tx_questions_only_apply_to_actual_deploy(self):
        deploy_state = SharedState(session_id="generic-deploy")
        deploy = IntentAlignmentCoordinator(self.engine, deploy_state)
        deploy.consume_text(
            "用 B210 发射 BPSK 信号，载频 915 MHz，采样率 2 Msps"
        )
        self.assertEqual(
            deploy_state.intent.missing_fields,
            ["duration_seconds", "success_conditions"],
        )
        self.assertIn("physical_tx", deploy_state.intent.specification.profile_refs)

        configure_state = SharedState(session_id="generic-configure")
        configure = IntentAlignmentCoordinator(self.engine, configure_state)
        configure.consume_text(
            "给 Pluto 配好发射流图，载频 915 MHz，采样率 2 Msps，先不要发射"
        )
        self.assertEqual(configure_state.intent.missing_fields, [])
        self.assertNotIn(
            "physical_tx", configure_state.intent.specification.profile_refs
        )

    def test_task_card_receives_shared_intent_snapshot(self):
        outcome = self.alignment.consume_text("构建 BPSK 过 AWGN 并测 EVM")
        self.assertIsNotNone(outcome.intent)
        workflow = self.engine.instantiate(outcome.intent, self.state)
        stage = self.engine.start_stage()
        card = make_task_card(workflow, stage, self.state, "")
        self.assertEqual(card.intent_id, self.state.intent.intent_id)
        self.assertEqual(
            card.inputs["shared_intent"]["semantic_hash"],
            self.state.intent.semantic_hash,
        )

    def test_spec_digest_is_scoped_to_current_intent_not_previous_project(self):
        self.state.project.config.update({
            "protocol": "ble", "local_name": "old-name", "hardware": "pluto",
        })
        self.state.intent.status = "confirmed"
        self.state.intent.task_type = "DIAGNOSE"
        self.state.intent.parameters = {"hardware": "b210"}
        self.state.intent.parameter_sources = {"hardware": "user"}
        self.state.intent.goals = ["诊断当前 B210 设备"]
        digest = self.state.spec_digest()
        self.assertEqual(digest["hardware"], "b210")
        self.assertEqual(digest["protocol"], "")
        self.assertEqual(digest["local_name"], "")
        self.assertNotIn("old-name", str(digest["radio_specification"]))

    def test_claim_summary_is_scoped_to_current_intent(self):
        self.state.intent = SharedIntent.new("first")
        ClaimStore(self.state).upsert(Claim(
            id="old-claim", statement="old task", layer="sim"
        ))
        self.assertEqual(
            len(ClaimStore(self.state).summary(active_intent_only=True)), 1
        )
        self.state.intent = SharedIntent.new("second")
        self.assertEqual(
            ClaimStore(self.state).summary(active_intent_only=True), []
        )
        self.assertEqual(len(ClaimStore(self.state).summary(active_intent_only=False)), 1)

    def test_stale_interaction_response_cannot_overwrite_new_intent(self):
        self.alignment.consume_text("我要用硬件发射一段ble信号")
        interaction = dict(self.state.intent.interaction)
        result = self.alignment.consume_response({
            "interaction_id": interaction["interaction_id"],
            "base_intent_revision": interaction["base_intent_revision"] - 1,
            "value": "pluto",
        })
        self.assertTrue(result.pending)
        self.assertNotIn("hardware", self.state.intent.parameters)
        self.assertIn("The intent revision has changed", result.message)

    def test_revision_impact_is_field_based(self):
        impact = analyze_intent_patch(
            {"hardware": "pluto", "local_name": "old"},
            {"hardware": "pluto", "local_name": "new"},
            runtime_active=True,
        )
        self.assertEqual(impact["scope"], "downstream")
        self.assertTrue(impact["requires_stop"])
        self.assertTrue(impact["requires_reconfirmation"])

    def test_physical_rf_path_is_never_invented_as_passed(self):
        from grc.agent.tools.diagnosis_checks import run_diagnosis_checks
        from grc.agent.tools.registry import ToolContext

        ctx = ToolContext(
            out_dir=self.temp.name,
            extra={
                "state": self.state,
                "shared_intent": {"parameters": {"hardware": "pluto"}},
            },
        )
        result = run_diagnosis_checks(
            ctx,
            device_type="pluto",
            dimensions=["rf_path"],
            live_probe=False,
        )
        finding = result["findings"][0]
        self.assertEqual(finding["status"], "unknown")
        self.assertTrue(finding["requires_human"])
        self.assertTrue(Path(result["report_path"]).is_file())


class PlannerRewiringContractTest(unittest.TestCase):
    """V6 P0-2: planner-materialized stages must not be dead code."""

    @staticmethod
    def _stage(sid, passed, interaction="autonomous"):
        from grc.agent.workflow.schema import Stage

        return Stage(
            id=sid,
            interaction=interaction,
            transitions={
                "passed": passed, "failed": "waiting_user", "errored": "stop"
            },
        )

    def test_rewire_inserted_chain_relinks_transitions(self):
        from grc.agent.workflow.plan_compiler import _rewire_inserted_chain

        stages = [
            self._stage("tx_build_and_validate", "hardware_precheck"),
            self._stage("hardware_precheck", "hardware_confirmation"),
            self._stage(
                "hardware_confirmation",
                "configure_and_check",
                interaction="checkpoint",
            ),
            self._stage("configure_and_check", "completed"),
        ]
        additions = [
            # Runtime-template fragment whose passed target only exists in
            # the runtime chain — a dangling id in this plan.
            self._stage("discover_and_probe_hardware", "rf_plan_confirmation"),
            self._stage("build_ble_advertiser", "offline_protocol_verify"),
            # Deploy-template fragment pointing at the legacy catalog id.
            self._stage("offline_protocol_verify", "discover_and_probe_device"),
        ]
        insert_at = 2
        successor_id = "hardware_confirmation"
        merged = stages[:insert_at] + additions + stages[insert_at:]
        _rewire_inserted_chain(merged, additions, insert_at, successor_id)

        by_id = {stage.id: stage for stage in merged}
        # Nothing may bypass the inserted chain.
        self.assertEqual(
            by_id["hardware_precheck"].transitions["passed"],
            "discover_and_probe_hardware",
        )
        # Dangling template targets follow the chain, then return to the
        # original successor.
        self.assertEqual(
            by_id["discover_and_probe_hardware"].transitions["passed"],
            "build_ble_advertiser",
        )
        self.assertEqual(
            by_id["offline_protocol_verify"].transitions["passed"],
            "hardware_confirmation",
        )
        # Valid in-chain targets are preserved.
        self.assertEqual(
            by_id["build_ble_advertiser"].transitions["passed"],
            "offline_protocol_verify",
        )

    def test_inserted_terminal_fragment_continues_to_successor(self):
        from grc.agent.workflow.plan_compiler import _rewire_inserted_chain

        predecessor = self._stage("build_ble_advertiser", "rf_plan_confirmation")
        inserted = self._stage("generic_builder", "completed")
        checkpoint = self._stage(
            "rf_plan_confirmation", "configure_device", interaction="checkpoint"
        )
        stages = [predecessor, inserted, checkpoint]
        _rewire_inserted_chain(stages, [inserted], 1, checkpoint.id)
        self.assertEqual(
            predecessor.transitions["passed"], "generic_builder"
        )
        self.assertEqual(
            inserted.transitions["passed"], "rf_plan_confirmation"
        )

    def test_protocol_builder_covers_generic_builder_by_predicates(self):
        from grc.agent.workflow.plan_compiler import compile_stages

        engine = WorkflowEngine(tempfile.mktemp(suffix=".json"))
        intent = WorkflowIntent(
            raw_text="Transmit a BLE advertisement with PlutoSDR",
            task_type="HARDWARE_CONFIGURE",
            capabilities=[
                "build_tx", "protocol", "hardware_configure",
                "hardware_runtime", "deploy",
            ],
            slots={
                "protocol": "ble", "hardware": "pluto", "direction": "tx",
                "operation": "deploy", "carrier_frequency": 2_402_000_000.0,
                "sample_rate": 2_000_000.0, "duration_seconds": 30.0,
            },
        )
        candidate = engine.catalog["task_candidates"]["HARDWARE_CONFIGURE"]
        stages = engine._compose_stages(intent, candidate)
        proposal = [
            {
                "id": "tx_build_and_validate",
                "stage_id": "tx_build_and_validate",
                "produces": [
                    "flowgraph_saved", "structural_validation_completed",
                ],
                "success_predicates": [
                    "flowgraph_saved", "structural_validation_completed",
                ],
            },
            {
                "id": "hardware_precheck",
                "stage_id": "hardware_precheck",
                "produces": ["hardware_precheck_completed"],
                "success_predicates": ["hardware_precheck_completed"],
            },
        ]
        compiled, _nodes, rejected, unbound = compile_stages(
            intent, stages, catalog=engine.catalog, proposal=proposal
        )
        ids = [stage.id for stage in compiled]
        self.assertEqual(rejected, [])
        self.assertEqual(unbound, [])
        self.assertNotIn("tx_build_and_validate", ids)
        self.assertIn("hardware_precheck", ids)
        by_id = {stage.id: stage for stage in compiled}
        self.assertEqual(
            by_id["discover_and_probe_hardware"].transitions["passed"],
            "rf_plan_confirmation",
        )
        self.assertEqual(
            by_id["hardware_precheck"].transitions["passed"],
            "discover_and_probe_hardware",
        )

    def test_runtime_guard_prevents_completion_with_deferred_work(self):
        from grc.agent.workflow.schema import Workflow

        engine = WorkflowEngine(tempfile.mktemp(suffix=".json"))
        finished = self._stage("first", "completed")
        finished.execution_status = "completed"
        finished.outcome = "passed"
        pending = self._stage("hardware_precheck", "completed")
        engine.workflow = Workflow(
            workflow_id="wf-guard",
            task_type="HARDWARE_CONFIGURE",
            intent=WorkflowIntent(raw_text="deploy"),
            stages=[finished, pending],
            current_stage="first",
            deferred_plan=[{"id": "rf_plan_confirmation"}],
        )
        engine._transition("completed")
        self.assertEqual(engine.workflow.execution_status, "pending")
        self.assertEqual(engine.workflow.current_stage, "hardware_precheck")
        self.assertEqual(engine.workflow.outcome, "")

    def test_reload_repairs_persisted_premature_completion(self):
        from grc.agent.workflow.schema import Workflow

        path = tempfile.mktemp(suffix=".json")
        engine = WorkflowEngine(path)
        finished = self._stage("generic_builder", "completed")
        finished.execution_status = "completed"
        finished.outcome = "passed"
        pending = self._stage("hardware_precheck", "rf_plan_confirmation")
        engine.workflow = Workflow(
            workflow_id="wf-reload-guard",
            task_type="HARDWARE_CONFIGURE",
            intent=WorkflowIntent(raw_text="deploy"),
            stages=[finished, pending],
            execution_status="completed",
            outcome="passed",
            current_stage=finished.id,
            deferred_plan=[{"id": "rf_plan_confirmation"}],
        )
        engine.save()
        restored = WorkflowEngine(path)
        self.assertEqual(restored.workflow.execution_status, "pending")
        self.assertEqual(restored.workflow.current_stage, "hardware_precheck")
        self.assertEqual(restored.workflow.outcome, "")

    def test_repair_dangling_transitions_redirects_illegal_targets(self):
        from grc.agent.workflow.plan_compiler import (
            _repair_dangling_transitions,
        )

        stages = [
            self._stage("build", "ghost_stage"),
            self._stage("verify", "another_ghost"),
        ]
        repairs = _repair_dangling_transitions(stages)
        self.assertEqual(stages[0].transitions["passed"], "verify")
        self.assertEqual(stages[1].transitions["passed"], "completed")
        self.assertEqual(len(repairs), 2)


class RequestedOperationsNormalizationTest(unittest.TestCase):
    """V6 P2-7: LLM operation objects must flatten to comparable strings."""

    def test_flattens_dicts_and_preserves_order(self):
        from grc.agent.workflow.engine import _normalize_requested_operations

        self.assertEqual(
            _normalize_requested_operations(
                [
                    {"operation": "deploy", "capability": "protocol"},
                    "save",
                    {"tool": "build_ble_1m_waveform"},
                ]
            ),
            ["deploy", "save", "build_ble_1m_waveform"],
        )

    def test_non_list_and_duplicates_are_safe(self):
        from grc.agent.workflow.engine import _normalize_requested_operations

        self.assertEqual(_normalize_requested_operations("junk"), [])
        self.assertEqual(_normalize_requested_operations(None), [])
        self.assertEqual(
            _normalize_requested_operations(
                [{"operation": "deploy"}, {"operation": "deploy"}]
            ),
            ["deploy"],
        )


if __name__ == "__main__":
    unittest.main()
