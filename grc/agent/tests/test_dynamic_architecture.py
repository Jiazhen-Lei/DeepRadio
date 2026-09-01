from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from grc.agent.service import session_store
from grc.agent.service.mainagent_service import ServiceAgent
from grc.agent.service.subagents import (
    build_common_constraints,
    build_orchestrator_prompt,
    subagent_names,
)
from grc.agent.state import SharedState
from grc.agent.tools import registry
from grc.agent.tools.hardware_profiles import resolve_hardware_profile
from grc.agent.tools.hardware_tools import _completion_satisfied, _rf_approved
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow.dynamic import DynamicWorkflowStore


def stage(status: str = "running") -> dict:
    return {
        "id": "design",
        "objective": "Build the requested flowgraph",
        "target_agent": "flowgraph_agent",
        "inputs": {"modulation": "bpsk"},
        "expected_evidence": ["artifact:grc_path"],
        "status": status,
        "result_refs": [],
    }


class DynamicWorkflowStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "workflow.json")
        self.store = DynamicWorkflowStore(self.path, subagent_names())
        self.workflow = self.store.begin_turn("Build BPSK", 0)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def update(self, stages, **overrides):
        data = {
            "intent_summary": "Build BPSK",
            "intent_slots": {"modulation": "bpsk"},
            "stages": stages,
            "current_stage": "design" if stages else "",
            "execution_status": "running",
            "task_type": "DYNAMIC",
            "expected_revision": self.store.workflow.revision,
            "events": [],
            "artifacts": {},
            "metrics": {},
            "project_version": 0,
        }
        data.update(overrides)
        return self.store.update(**data)

    def test_mainagent_can_replace_and_reorder_the_complete_plan(self):
        first = self.update([stage()])
        revised = self.update(
            [
                {
                    "id": "validate",
                    "objective": "Validate",
                    "target_agent": "verification_agent",
                    "expected_evidence": ["validate_flowgraph"],
                    "status": "pending",
                },
                stage(),
            ],
            current_stage="validate",
            expected_revision=first.revision,
        )
        self.assertEqual([item.id for item in revised.stages], ["validate", "design"])
        self.assertNotIn("execution_mode", revised.to_dict()["stages"][0])
        self.assertNotIn("transitions", revised.to_dict()["stages"][0])

    def test_stale_revision_is_rejected(self):
        self.update([stage()])
        with self.assertRaisesRegex(ValueError, "Stale Workflow revision"):
            self.update([stage()], expected_revision=1)

    def test_stage_completion_requires_host_observed_evidence(self):
        running = self.update([stage()])
        with self.assertRaisesRegex(ValueError, "lacks verified evidence"):
            self.update(
                [stage("completed")], expected_revision=running.revision
            )
        completed = self.update(
            [stage("completed")],
            expected_revision=running.revision,
            artifacts={"grc_path": "/session/final/radio.grc"},
            execution_status="completed",
        )
        self.assertEqual(completed.execution_status, "completed")

    def test_user_decision_is_recorded_but_does_not_grant_permission(self):
        self.update([stage()])
        checkpoint = self.store.request_decision(
            stage_id="design",
            question="Start RF?",
            purpose="rf_authorization",
            permission="rf.start",
        )
        self.assertEqual(self.store.digest()["wait_kind"], "approval")
        self.assertNotIn("granted", checkpoint)
        resolved = self.store.resolve_decision(checkpoint["id"], "approved")
        self.assertEqual(resolved["status"], "approved")

    def test_interrupted_running_stage_recovers_as_pending(self):
        self.update([stage()])
        recovered = DynamicWorkflowStore(self.path, subagent_names())
        self.assertEqual(recovered.workflow.execution_status, "pending")
        self.assertEqual(recovered.workflow.stage("design").status, "pending")

    def test_corrupted_workflow_stops_instead_of_overwriting_state(self):
        Path(self.path).write_text("{broken", encoding="utf-8")
        corrupted = DynamicWorkflowStore(self.path, subagent_names())
        with self.assertRaisesRegex(RuntimeError, "could not be loaded"):
            corrupted.begin_turn("replace it", 0)
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), "{broken")


class PermissionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        registry.load_all()
        self.ctx = ToolContext()
        self.ctx.extra["state"] = SharedState(session_id="permission")

    def test_permissions_are_explicit_and_stop_is_always_available(self):
        self.assertEqual(
            registry.action_metadata("start_flowgraph")["permission"], "rf.start"
        )
        self.assertEqual(
            registry.action_metadata("stop_flowgraph")["permission"], "rf.stop"
        )
        self.ctx.extra["forbidden_permissions"] = ["rf.stop"]
        stopped = registry.call("stop_flowgraph", {}, self.ctx)
        self.assertNotEqual(stopped.get("policy"), "DENY")

    def test_read_only_request_blocks_project_write(self):
        self.ctx.extra["mutation_forbidden"] = True
        result = registry.call("spec_commit", {"text": "test"}, self.ctx)
        self.assertEqual(result.get("policy"), "DENY")
        self.assertEqual(result.get("permission"), "project.write")

    def test_rf_start_fails_closed_without_runtime_and_user_grant(self):
        result = registry.call("start_flowgraph", {"grc_path": "missing.grc"}, self.ctx)
        self.assertEqual(result.get("policy"), "DENY")
        self.assertIn("Missing execution preconditions", result.get("error", ""))

    def test_rf_permission_is_bound_to_workflow_and_project_version(self):
        state = self.ctx.extra["state"]
        state.runtime.granted_permissions.append("rf.start")
        state.project.flowgraph_version = 3
        state.project.config["rf_permission_grant"] = {
            "workflow_id": "wf-current",
            "project_version": 3,
        }
        self.ctx.extra["workflow"] = {"workflow_id": "wf-current"}
        self.assertTrue(_rf_approved(self.ctx))
        self.ctx.extra["workflow"] = {"workflow_id": "wf-other"}
        self.assertFalse(_rf_approved(self.ctx))
        self.ctx.extra["workflow"] = {"workflow_id": "wf-current"}
        state.project.flowgraph_version = 4
        self.assertFalse(_rf_approved(self.ctx))

    def test_device_probe_evidence_survives_the_agent_turn(self):
        state = self.ctx.extra["state"]
        state.project.config["observed_device"] = {
            "type": "b210",
            "identity": "serial=TEST",
        }
        self.assertTrue(_completion_satisfied(self.ctx, "device_probed"))


class DomainSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ctx = ToolContext(out_dir=self.temp.name)
        self.ctx.extra["state"] = SharedState(session_id="domain-safety")
        registry.load_all()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_ble_packet_is_built_and_verified_by_tools(self):
        packet = registry.call(
            "build_ble_advertising_pdu",
            {"local_name": "deepradio", "channel": 37},
            self.ctx,
        )
        waveform = registry.call(
            "generate_ble_1m_waveform",
            {"local_name": "deepradio", "channel": 37},
            self.ctx,
        )
        verified = registry.call(
            "verify_ble_packet_bits",
            {"local_name": "deepradio", "channel": 37},
            self.ctx,
        )
        self.assertTrue(packet.get("ok"), packet)
        self.assertTrue(waveform.get("ok"), waveform)
        self.assertTrue(verified.get("valid"), verified)

    def test_hardware_range_remains_host_validated(self):
        b210 = resolve_hardware_profile("b210")
        self.assertIsNotNone(b210)
        low, high = b210.frequency_range
        self.assertLessEqual(low, 2.4e9)
        self.assertGreaterEqual(high, 2.4e9)
        self.assertFalse(low <= 7e9 <= high)


class MainAgentServiceContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.sessions = str(Path(self.temp.name) / "sessions")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prompts_are_short_and_roles_are_separated(self):
        main = build_orchestrator_prompt(subagent_names())
        sub = build_common_constraints()
        self.assertIn("唯一用户接口", main)
        self.assertIn("不与用户交互", sub)
        self.assertNotIn("hybrid", main.lower())
        self.assertNotIn("deterministic", main.lower())
        self.assertLess(len(main), 600)

    def test_service_keeps_ui_contract_and_uses_mainagent_plan(self):
        class FakeAgent:
            def __init__(self, ctx):
                self.ctx = ctx

            def invoke(self, _payload, _config):
                workflow = self.ctx.extra["workflow_store"]
                workflow.update(
                    intent_summary="Build BPSK",
                    intent_slots={"modulation": "bpsk"},
                    stages=[stage()],
                    current_stage="design",
                    execution_status="running",
                    task_type="DYNAMIC",
                    expected_revision=workflow.workflow.revision,
                    events=[],
                    artifacts={},
                    metrics={},
                    project_version=0,
                )
                return {"messages": [{"role": "assistant", "content": "Workflow created."}]}

        with mock.patch.object(
            session_store, "sessions_root", return_value=self.sessions
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent",
            side_effect=lambda ctx: FakeAgent(ctx),
        ):
            agent = ServiceAgent(session_id="dynamic-ui")
            reply = agent.step("Build BPSK")
        self.assertEqual(reply.text, "Workflow created.")
        self.assertEqual(reply.workflow_digest["current_stage"], "design")
        self.assertEqual(reply.workflow_digest["stage_total"], 1)
        self.assertTrue(callable(agent.step_command))
        self.assertTrue(hasattr(reply, "spec_digest"))

    def test_missing_mainagent_does_not_enter_another_workflow(self):
        with mock.patch.object(
            session_store, "sessions_root", return_value=self.sessions
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            reply = ServiceAgent(session_id="no-llm").step("Build BPSK")
        self.assertEqual(reply.stage, "ERROR")
        self.assertIn("no longer switches", reply.text)

    def test_legacy_permission_state_loads_into_explicit_permissions(self):
        path = Path(self.temp.name) / "state.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": "legacy",
                    "runtime": {
                        "requested_effect": "RF_RUN",
                        "granted_effects": ["RF_RUN"],
                    },
                    "decisions": [
                        {
                            "decision_id": "decision-1",
                            "key": "rf",
                            "value": "approved",
                            "source": "gui",
                            "effect_level": "RF_RUN",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        state = SharedState.load(str(path), session_id="legacy")
        self.assertEqual(state.runtime.requested_permission, "RF_RUN")
        self.assertEqual(state.runtime.granted_permissions, ["RF_RUN"])
        self.assertEqual(state.decisions[0].permission, "RF_RUN")


if __name__ == "__main__":
    unittest.main()
