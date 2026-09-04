import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grc.agent.memory.profile import UserProfile
from grc.agent.service import MainAgentRuntime, build_mainagent_runtime
from grc.agent.service import result_projector, session_store
from grc.agent.service.tools_lc import _call_registry, _wrap_spec
from grc.agent.service.workflow_tools import (
    _materialize_stage_plan,
    _normalize_intent_slots,
    build_workflow_tools,
)
from grc.agent.state import Claim, ClaimStore, SharedIntent, SharedState
from grc.agent.schema import AgentReply, ToolInvocation
from grc.agent.tools import registry
from grc.agent.tools.build_tools import _missing_literal_file_source
from grc.agent.tools.critic_tools import _missing_file_sources
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow.dynamic import DynamicWorkflowStore, missing_evidence


def _spec_stage(status="running"):
    return {
        "id": "radio_specification_alignment",
        "status": status,
    }


def _radio_design_stage(status="pending"):
    return {
        "id": "radio_design",
        "status": status,
    }


class MainAgentRuntimeTest(unittest.TestCase):
    def test_presentation_settings_are_fixed_and_language_is_explicit(self):
        profile = UserProfile()
        self.assertEqual((profile.level, profile.language), ("practitioner", "en"))

        profile.configure("beginner", "cn")

        self.assertEqual((profile.level, profile.language), ("beginner", "cn"))
        self.assertIn("简体中文", profile.style_prompt())
        self.assertIn("wording only", profile.style_prompt())
        self.assertEqual(profile.text("English", "中文"), "中文")

    def test_unknown_presentation_values_do_not_change_selection(self):
        profile = UserProfile(level="expert", language="cn")
        profile.configure("automatic", "fr")
        self.assertEqual((profile.level, profile.language), ("expert", "cn"))

    def test_empty_intent_slots_are_normalized(self):
        self.assertEqual(_normalize_intent_slots(None), {})
        self.assertEqual(_normalize_intent_slots({"protocol": "BLE"}), {
            "protocol": "BLE"
        })
        for invalid in ("", "BLE", []):
            with self.assertRaises(ValueError):
                _normalize_intent_slots(invalid)

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_core"), "langchain_core is not installed"
    )
    def test_registry_tool_schema_preserves_nested_enums(self):
        registry.load_all()
        spec = next(item for item in registry.all_specs() if item.name == "spec_update")
        tool = _wrap_spec(spec, ToolContext())
        fields = tool.tool_call_schema["properties"]["fields"]["items"]["properties"]

        self.assertEqual(fields["group"]["enum"], ["required", "added"])
        self.assertEqual(
            fields["status"]["enum"],
            ["aligned", "needs_confirmation", "missing"],
        )

    def test_stage_plan_uses_fixed_library_contract(self):
        catalog = {
            "flowgraph_verification": {
                "objective": "Verify the flowgraph",
                "skills": ["grc-critic"],
                "allowed_tools": ["validate_flowgraph"],
                "expected_evidence": ["validate_flowgraph"],
            },
        }
        with patch(
            "grc.agent.service.workflow_tools.load_stage_catalog",
            return_value=catalog,
        ):
            stages, _ = _materialize_stage_plan([{
                "id": "flowgraph_verification",
                "objective": "Verify the generated flowgraph",
                "status": "pending",
            }])

        self.assertEqual(stages[0]["objective"], "Verify the flowgraph")
        self.assertEqual(stages[0]["skills"], ["grc-critic"])
        self.assertEqual(stages[0]["expected_evidence"], ["validate_flowgraph"])

    def test_turn_keeps_the_agent_reply_contract(self):
        captured = {}

        class FakeMainAgent:
            def invoke(self, agent_input, _config):
                captured.update(agent_input)
                return {"messages": [{"content": "runtime reply"}]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(session_store, "ensure_run_metadata"), patch.object(
                session_store, "state_path", return_value=str(root / "state.json")
            ), patch.object(
                session_store, "workflow_path", return_value=str(root / "workflow.json")
            ), patch.object(
                session_store, "session_root", return_value=str(root)
            ), patch.object(
                session_store, "nested_export_dir", return_value=str(root / "work")
            ), patch.object(
                session_store, "append_session_event"
            ), patch.object(
                session_store, "recent_events", return_value=[]
            ), patch.object(
                session_store, "read_named_artifacts", return_value={
                    "waveform_path": "/tmp/session/final/ble/waveform.cfile"
                }
            ), patch.object(
                session_store,
                "write_artifact_manifest",
                return_value=str(root / "manifest.json"),
            ), patch(
                "grc.agent.service.mainagent_runtime.orch.build_agent",
                return_value=FakeMainAgent(),
            ):
                runtime = build_mainagent_runtime(
                    session_id="runtime-test", platform=object()
                )
                reply = runtime.step("hello")

        self.assertEqual(reply.text, "runtime reply")
        self.assertEqual(reply.stage, "DELIVER")
        self.assertIsInstance(reply.spec_digest, dict)
        self.assertIsInstance(reply.workflow_digest, dict)
        self.assertIn(
            '"waveform_path": "/tmp/session/final/ble/waveform.cfile"',
            captured["messages"][0]["content"],
        )

    def test_public_runtime_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(session_store, "ensure_run_metadata"), patch.object(
                session_store, "state_path", return_value=str(root / "state.json")
            ), patch.object(
                session_store, "workflow_path", return_value=str(root / "workflow.json")
            ):
                runtime = build_mainagent_runtime(
                    session_id="runtime-test", platform=object()
                )

        self.assertIsInstance(runtime, MainAgentRuntime)
        self.assertFalse(hasattr(runtime, "ctx"))
        self.assertNotIn("workflow_id", runtime.workflow_digest())
        self.assertEqual(runtime.intent_slots(), {})

    def test_runtime_digest_exposes_current_hardware_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(session_store, "ensure_run_metadata"), patch.object(
                session_store, "state_path", return_value=str(root / "state.json")
            ), patch.object(
                session_store, "workflow_path", return_value=str(root / "workflow.json")
            ), patch.object(session_store, "recent_events", return_value=[]):
                runtime = build_mainagent_runtime(
                    session_id="runtime-test", platform=object()
                )
                workflow = runtime._workflow.begin_turn("Use a PlutoSDR", 0)
                runtime._state.intent.capabilities = ["hardware_configure"]
                runtime._state.project.config["hardware_detection"] = {
                    "state": "detected",
                    "workflow_id": workflow.workflow_id,
                }
                runtime._state.project.config["observed_device"] = {
                    "type": "pluto", "identity": "usb:1.2.3"
                }
                digest = runtime.workflow_digest()

        self.assertEqual(digest["capabilities"], ["hardware_configure"])
        self.assertEqual(
            digest["shared_intent"]["intent_id"], runtime._state.intent.intent_id
        )
        self.assertEqual(digest["hardware_detection"]["state"], "detected")
        self.assertEqual(digest["observed_device"]["identity"], "usb:1.2.3")

    def test_claims_bind_to_the_current_stage_and_invalidate_by_dependency(self):
        runtime = MainAgentRuntime.__new__(MainAgentRuntime)
        runtime._state = SharedState(session_id="runtime-test")
        runtime._state.intent = SharedIntent.new("Build a radio", "wf-test")
        runtime._workflow = SimpleNamespace(
            workflow=SimpleNamespace(current_stage="flowgraph_verification")
        )
        runtime._record_claim(
            "flowgraph_valid", "Flowgraph is valid", "flowgraph",
            "validate_flowgraph", {"valid": True}, True,
        )
        runtime._workflow.workflow.current_stage = "hardware_preparation"
        runtime._record_claim(
            "device_probed", "Device was probed", "hardware",
            "probe_device", {"identity": "usb:test"}, True,
        )

        invalidated = ClaimStore(runtime._state).invalidate_scopes(
            ["flowgraph"], "workflow reopened"
        )

        self.assertEqual(invalidated, ["flowgraph_valid"])
        self.assertEqual(runtime._state.claims[0].status, "Supported")
        self.assertEqual(runtime._state.claims[0].freshness, "Stale")
        self.assertEqual(runtime._state.claims[1].status, "Supported")
        self.assertEqual(runtime._state.claims[1].freshness, "Current")
        self.assertEqual(runtime._state.claims[1].producer, "hardware_preparation")

    def test_workflow_declares_claims_before_tools_run(self):
        state = SharedState(session_id="runtime-test")
        state.intent = SharedIntent.new("Build a radio", "wf-test")
        workflow = SimpleNamespace(stages=[
            SimpleNamespace(id="flowgraph_verification")
        ])
        definition = {"claims": [{
            "id": "final_flowgraph_valid",
            "scope": "flowgraph",
            "statement": "Current flowgraph is structurally valid.",
        }]}

        with patch(
            "grc.agent.workflow.catalog.load_stage_catalog",
            return_value={"flowgraph_verification": definition},
        ):
            created = ClaimStore(state).ensure_for_workflow(workflow)

        self.assertEqual(created, ["final_flowgraph_valid"])
        self.assertEqual(state.claims[0].status, "Untested")
        self.assertEqual(state.claims[0].producer, "flowgraph_verification")

    def test_rf_lifecycle_does_not_create_or_flip_safety_claims(self):
        runtime = MainAgentRuntime.__new__(MainAgentRuntime)
        runtime._state = SharedState(session_id="runtime-test")
        runtime._state.intent = SharedIntent.new("Run RF", "wf-test")
        runtime._workflow = SimpleNamespace(
            workflow=SimpleNamespace(current_stage="physical_rf_execution")
        )
        reply = AgentReply(tool_invocations=[
            ToolInvocation(name="start_flowgraph", result={
                "ok": True, "running": True, "ready": True,
                "startup_health_passed": True, "run_id": "run-test",
            }),
            ToolInvocation(name="query_runtime_status", result={
                "ok": False, "running": False, "run_id": "run-test",
                "reason": "crashed", "return_code": 1, "crashed": True,
            }),
        ])

        result_projector.project_tool_results(
            runtime._state,
            reply,
            record_claim=runtime._record_claim,
            semantic_hash=lambda _path: "hash",
        )

        claims = {claim.id: claim for claim in runtime._state.claims}
        self.assertEqual(set(claims), {"bounded_runtime_healthy"})
        self.assertEqual(claims["bounded_runtime_healthy"].status, "Contradicted")
        self.assertEqual(len(claims["bounded_runtime_healthy"].evidence), 2)

    def test_flowgraph_validation_projects_scoped_claim(self):
        runtime = MainAgentRuntime.__new__(MainAgentRuntime)
        runtime._state = SharedState(session_id="runtime-test")
        runtime._state.intent = SharedIntent.new("Validate radio", "wf-test")
        runtime._workflow = SimpleNamespace(
            workflow=SimpleNamespace(current_stage="flowgraph_verification")
        )
        reply = AgentReply(tool_invocations=[ToolInvocation(
            name="validate_flowgraph",
            result={"ok": True, "valid": True, "errors": []},
        )])

        result_projector.project_tool_results(
            runtime._state, reply,
            record_claim=runtime._record_claim,
            semantic_hash=lambda _path: "hash",
        )

        claim = ClaimStore(runtime._state).get("final_flowgraph_valid")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.layer, "flowgraph")
        self.assertEqual(claim.status, "Supported")

    def test_ota_checkpoint_records_task_evidence_bound_to_run(self):
        runtime = MainAgentRuntime.__new__(MainAgentRuntime)
        runtime.session_id = "runtime-test"
        runtime._state = SharedState(session_id="runtime-test")
        runtime._state.intent = SharedIntent.new("Send BLE", "wf-test")
        runtime._state.project.config["runtime"] = {
            "run_id": "run-test", "status": "running", "running": True,
        }
        ClaimStore(runtime._state).upsert(Claim(
            id="success_condition_1",
            statement="Phone observes Local Name syx",
            layer="task",
            producer="over_air_verification",
        ))
        runtime._workflow = SimpleNamespace(
            workflow=SimpleNamespace(workflow_id="wf-test"),
            resolve_decision=lambda _checkpoint_id, _decision: {
                "id": "checkpoint-test",
                "purpose": "ota_observation",
                "permission": "",
                "stage_id": "over_air_verification",
            },
        )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            session_store, "state_path", return_value=str(Path(directory) / "state.json")
        ), patch.object(session_store, "append_session_event"), patch.object(
            runtime, "_invoke_mainagent", return_value="continued"
        ):
            reply = runtime._resolve_checkpoint({
                "checkpoint_id": "checkpoint-test",
                "decision": "approved",
                "observation": {"observed_name": "syx"},
            })

        claim = ClaimStore(runtime._state).get("success_condition_1")
        self.assertEqual(reply, "continued")
        self.assertEqual(claim.status, "Supported")
        self.assertEqual(claim.evidence[-1].observation["run_id"], "run-test")

    def test_hardware_tool_results_are_projected(self):
        state = SharedState(session_id="runtime-test")
        state.intent = SharedIntent.new("Use a PlutoSDR", "wf-test")
        reply = AgentReply(tool_invocations=[
            ToolInvocation(name="discover_devices", result={
                "ok": True,
                "device_found": True,
                "device_type": "pluto",
                "device_identity": "usb:1.2.3",
            }),
            ToolInvocation(name="probe_device", result={
                "ok": True,
                "device_probed": True,
                "device_type": "pluto",
                "device_identity": "usb:1.2.3",
            }),
        ])

        result_projector.project_tool_results(
            state,
            reply,
            record_claim=lambda *args, **kwargs: None,
            semantic_hash=lambda _path: "hash",
        )

        self.assertEqual(state.project.config["hardware_detection"]["state"], "detected")
        self.assertEqual(state.project.config["observed_device"]["identity"], "usb:1.2.3")

        reply.tool_invocations = [ToolInvocation(name="discover_devices", result={
            "ok": True,
            "device_found": False,
            "device_type": "pluto",
        })]
        result_projector.project_tool_results(
            state,
            reply,
            record_claim=lambda *args, **kwargs: None,
            semantic_hash=lambda _path: "hash",
        )
        self.assertEqual(state.project.config["hardware_detection"]["state"], "not_found")
        self.assertNotIn("observed_device", state.project.config)

        reply.tool_invocations = [ToolInvocation(name="probe_device", result={
            "ok": False,
            "device_probed": False,
            "error": "probe failed",
        })]
        result_projector.project_tool_results(
            state,
            reply,
            record_claim=lambda *args, **kwargs: None,
            semantic_hash=lambda _path: "hash",
        )
        self.assertEqual(state.project.config["hardware_detection"]["state"], "failed")

    def test_workflow_store_owns_retry_and_project_version(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(str(Path(directory) / "workflow.json"))
            workflow.begin_turn("Build a radio", 0)
            workflow.update(
                intent_summary="Build a radio",
                intent_slots={},
                stages=[_spec_stage()],
                current_stage="radio_specification_alignment",
                execution_status="running",
                task_type="DYNAMIC",
                expected_revision=1,
                events=[],
                artifacts={},
                metrics={},
                project_version=0,
            )
            revision = workflow.workflow.revision

            self.assertTrue(workflow.retry_current_stage())
            self.assertEqual(workflow.workflow.revision, revision + 1)
            self.assertEqual(workflow.current_stage().status, "pending")
            self.assertTrue(workflow.bind_project_version(3))
            self.assertEqual(workflow.workflow.base_project_version, 3)
            self.assertEqual(workflow.workflow.revision, revision + 1)

    def test_completed_stage_waits_for_the_next_user_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(str(Path(directory) / "workflow.json"))
            workflow.begin_turn("Build a radio", 0)
            workflow.update(
                intent_summary="Build a radio",
                intent_slots={},
                stages=[_spec_stage()],
                current_stage="radio_specification_alignment",
                execution_status="running",
                task_type="DYNAMIC",
                expected_revision=1,
                events=[],
                artifacts={},
                metrics={},
                project_version=0,
            )
            result = workflow.update(
                intent_summary="Build a radio",
                intent_slots={},
                stages=[_spec_stage("completed")],
                current_stage="radio_specification_alignment",
                execution_status="running",
                task_type="DYNAMIC",
                expected_revision=2,
                events=[{"kind": "spec_commit", "payload": {"ok": True}}],
                artifacts={},
                metrics={},
                project_version=0,
            )

        self.assertEqual(result.execution_status, "pending")

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_core"), "langchain_core is not installed"
    )
    def test_workflow_revision_does_not_change_intent_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(str(Path(directory) / "workflow.json"))
            workflow.begin_turn("Build a radio", 0)
            state = SharedState(session_id="runtime-test")
            state.intent = SharedIntent.new(
                "Build a radio", workflow.workflow.workflow_id
            )
            ctx = ToolContext(extra={
                "workflow_store": workflow,
                "session_id": "runtime-test",
                "state": state,
                "events": [],
                "artifacts": {},
                "metrics": {},
            })
            tools = {tool.name: tool for tool in build_workflow_tools(ctx, workflow)}
            with patch.object(session_store, "append_session_event"):
                created = json.loads(tools["update_workflow"].invoke({
                    "intent_summary": "Build a radio",
                    "stages": [_spec_stage()],
                    "current_stage": "radio_specification_alignment",
                    "expected_revision": 1,
                }))
                updated = json.loads(tools["update_current_stage"].invoke({
                    "stage_id": "radio_specification_alignment",
                    "status": "running",
                    "expected_revision": 2,
                }))

        self.assertTrue(created["ok"])
        self.assertTrue(updated["ok"])
        self.assertEqual(state.intent.revision, 1)

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_core"), "langchain_core is not installed"
    )
    def test_next_stage_cannot_start_in_the_same_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(str(Path(directory) / "workflow.json"))
            workflow.begin_turn("Build a radio", 0)
            workflow.update(
                intent_summary="Build a radio",
                intent_slots={},
                stages=[_spec_stage(), _radio_design_stage()],
                current_stage="radio_specification_alignment",
                execution_status="running",
                task_type="DYNAMIC",
                expected_revision=1,
                events=[],
                artifacts={},
                metrics={},
                project_version=0,
            )
            ctx = ToolContext(extra={
                "workflow_store": workflow,
                "session_id": "runtime-test",
                "events": [{"kind": "spec_commit", "payload": {"ok": True}}],
                "artifacts": {},
                "metrics": {},
            })
            tools = {tool.name: tool for tool in build_workflow_tools(ctx, workflow)}
            with patch.object(session_store, "append_session_event"):
                completed = json.loads(tools["update_current_stage"].invoke({
                    "stage_id": "radio_specification_alignment",
                    "status": "completed",
                    "expected_revision": 2,
                    "result_refs": ["spec_commit"],
                }))
                blocked = json.loads(tools["update_current_stage"].invoke({
                    "stage_id": "radio_design",
                    "status": "running",
                    "expected_revision": 3,
                }))

        self.assertTrue(completed["ok"])
        self.assertFalse(blocked["ok"])
        self.assertEqual(workflow.workflow.current_stage, "radio_specification_alignment")

    def test_stage_update_preserves_the_workflow_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(str(Path(directory) / "workflow.json"))
            workflow.begin_turn("Build a radio", 0)
            workflow.update(
                intent_summary="Build a radio",
                intent_slots={},
                stages=[_spec_stage(), _radio_design_stage()],
                current_stage="radio_specification_alignment",
                execution_status="running",
                task_type="DYNAMIC",
                expected_revision=1,
                events=[],
                artifacts={},
                metrics={},
                project_version=0,
            )
            workflow.update_stage(
                stage_id="radio_specification_alignment",
                status="completed",
                inputs=None,
                result_refs=["spec_commit"],
                expected_revision=2,
                events=[{"kind": "spec_commit", "payload": {"ok": True}}],
                artifacts={},
                metrics={},
                project_version=0,
            )
            result = workflow.update_stage(
                stage_id="radio_design",
                status="running",
                inputs={"protocol": "BLE"},
                result_refs=None,
                expected_revision=3,
                events=[],
                artifacts={},
                metrics={},
                project_version=0,
            )

        self.assertEqual(result.current_stage, "radio_design")
        self.assertEqual(result.stages[0].status, "completed")
        self.assertEqual(result.stages[1].skills, ["grc-ble-advertising", "grc-block-rag"])
        self.assertEqual(result.stages[1].inputs, {"protocol": "BLE"})

    def test_stage_gateway_rejects_cross_stage_tool(self):
        ctx = ToolContext(extra={
            "enforce_stage_tools": True,
            "stage_id": "flowgraph_verification",
            "workflow": {"current_stage": "flowgraph_verification"},
        })

        result = registry.call("spec_commit", {}, ctx)

        self.assertFalse(result["ok"])
        self.assertEqual(result["policy"], "DENY")
        self.assertIn("not allowed", result["error"])

    def test_stage_gateway_allows_internal_calls_from_allowed_tool(self):
        ctx = ToolContext(extra={
            "enforce_stage_tools": True,
            "stage_id": "diagnosis",
            "workflow": {"current_stage": "diagnosis"},
        })

        result = registry.call("debug_by_metric", {"metric": "evm"}, ctx)

        self.assertFalse(result["ok"])
        self.assertNotIn("not allowed", result["error"])

    def test_registry_tool_leaves_persistence_to_the_runtime(self):
        state = SharedState(session_id="runtime-test")
        state.intent = SharedIntent.new("Build a BLE transmitter", "wf-test")
        ctx = ToolContext(extra={"state": state, "state_path": "/unused/state.json"})
        with patch.object(state, "save") as save:
            result = json.loads(_call_registry(ctx, "spec_update", {
                "fields": [{
                    "key": "goal",
                    "label": "Goal",
                    "value": "Build a BLE transmitter",
                    "group": "required",
                    "source": "user",
                    "status": "aligned",
                }],
            }))

        self.assertTrue(result["ok"])
        save.assert_not_called()
        self.assertEqual(state.intent.specification.field("goal").status, "aligned")

    def test_named_artifacts_survive_later_manifest_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            waveform = root / "final" / "ble" / "waveform.cfile"
            waveform.parent.mkdir(parents=True)
            waveform.write_bytes(b"iq")
            grc_path = root / "final" / "radio.grc"
            grc_path.write_text("options: {}\n", encoding="utf-8")
            with patch.object(
                session_store, "session_root", return_value=str(root)
            ), patch.object(
                session_store, "ensure_run_metadata", return_value=""
            ):
                session_store.write_artifact_manifest(
                    "runtime-test", {"waveform_path": str(waveform)}
                )
                session_store.write_artifact_manifest(
                    "runtime-test", {"grc_path": str(grc_path)}
                )
                named = session_store.read_named_artifacts("runtime-test")

        self.assertEqual(named["waveform_path"], str(waveform))
        self.assertEqual(named["grc_path"], str(grc_path))

    def test_failed_validation_is_not_completion_evidence(self):
        missing = missing_evidence(
            ["validate_flowgraph"],
            [{"kind": "validate", "payload": {"ok": True, "valid": False}}],
            {},
            {},
        )
        self.assertEqual(missing, ["validate_flowgraph"])
        self.assertEqual(
            missing_evidence(
                ["validate_flowgraph"],
                [{"kind": "validate", "payload": {"ok": True, "valid": True}}],
                {},
                {},
            ),
            [],
        )

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_core"), "langchain_core is not installed"
    )
    def test_failed_stage_notifies_progress_and_finishes_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(str(Path(directory) / "workflow.json"))
            workflow.begin_turn("Build a radio", 0)
            updates = []
            ctx = ToolContext(extra={
                "state": SharedState(session_id="runtime-test"),
                "events": [],
                "artifacts": {},
                "metrics": {},
                "on_workflow_updated": updates.append,
            })
            update = build_workflow_tools(ctx, workflow)[0]
            with patch.object(session_store, "append_session_event"):
                update.invoke({
                    "intent_summary": "Build a radio",
                    "stages": [_spec_stage()],
                    "current_stage": "radio_specification_alignment",
                    "execution_status": "running",
                    "expected_revision": 1,
                })
                result = json.loads(update.invoke({
                    "intent_summary": "Build a radio",
                    "stages": [_spec_stage("failed")],
                    "current_stage": "radio_specification_alignment",
                    "execution_status": "running",
                    "expected_revision": 2,
                }))

        self.assertTrue(result["turn_complete"])
        self.assertEqual(result["workflow"]["execution_status"], "errored")
        self.assertEqual(updates[-1], "workflow_updated")

    def test_missing_file_source_is_rejected_and_reported(self):
        missing_path = "/session/work/build/missing.complex"
        self.assertEqual(
            _missing_literal_file_source(
                "blocks_file_source", {"file": missing_path}
            ),
            missing_path,
        )
        parameter = SimpleNamespace(get_value=lambda: missing_path)
        block = SimpleNamespace(
            key="blocks_file_source", params={"file": parameter}
        )
        ctx = ToolContext(blocks={"source": block})
        self.assertEqual(
            _missing_file_sources(ctx),
            [f"File Source input does not exist: {missing_path}"],
        )


if __name__ == "__main__":
    unittest.main()
