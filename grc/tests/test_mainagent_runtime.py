import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grc.agent.service import MainAgentRuntime, build_mainagent_runtime
from grc.agent.service import session_store
from grc.agent.service.tools_lc import _call_registry
from grc.agent.service.workflow_tools import (
    _materialize_stage_plan,
    _normalize_intent_slots,
    build_workflow_tools,
)
from grc.agent.state import SharedIntent, SharedState
from grc.agent.tools.build_tools import _missing_literal_file_source
from grc.agent.tools.critic_tools import _missing_file_sources
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow.dynamic import DynamicWorkflowStore, missing_evidence


def _spec_stage(status="running"):
    return {
        "id": "radio_specification_alignment",
        "objective": "Align the Radio Specification",
        "status": status,
        "tasks": [{
            "id": "align_radio_specification",
            "objective": "Align the Radio Specification",
            "target_agent": "spec_agent",
            "expected_evidence": ["spec_commit"],
            "status": status,
        }],
    }


def _radio_design_stage(status="pending"):
    return {
        "id": "radio_design",
        "objective": "Build the transmit waveform",
        "status": status,
        "tasks": [{
            "id": "build_transmit_waveform",
            "objective": "Build the transmit waveform",
            "target_agent": "radio_design_agent",
            "expected_evidence": [
                "artifact:tx_data",
                "artifact:waveform_path",
                "artifact:waveform_manifest",
            ],
            "status": status,
        }],
    }


class MainAgentRuntimeTest(unittest.TestCase):
    def test_empty_intent_slots_are_normalized(self):
        self.assertEqual(_normalize_intent_slots(""), {})
        self.assertEqual(_normalize_intent_slots(None), {})
        self.assertEqual(_normalize_intent_slots({"protocol": "BLE"}), {
            "protocol": "BLE"
        })
        with self.assertRaises(ValueError):
            _normalize_intent_slots("BLE")

    def test_stage_plan_uses_fixed_library_task(self):
        catalog = {
            "flowgraph_verification": {
                "objective": "Verify the flowgraph",
                "target_agent": "verification_agent",
                "task": {
                    "id": "verify_flowgraph",
                    "objective": "Validate the current flowgraph",
                    "expected_evidence": ["validate_flowgraph"],
                },
            },
        }
        with patch(
            "grc.agent.service.workflow_tools._load_stage_catalog",
            return_value=catalog,
        ):
            stages, _ = _materialize_stage_plan([{
                "id": "flowgraph_verification",
                "objective": "Verify the generated flowgraph",
                "status": "pending",
                "tasks": [{"target_agent": "wrong_agent"}],
            }])

        task = stages[0]["tasks"][0]
        self.assertEqual(task["id"], "verify_flowgraph")
        self.assertEqual(task["target_agent"], "verification_agent")
        self.assertEqual(task["expected_evidence"], ["validate_flowgraph"])

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

    def test_workflow_store_owns_retry_and_project_version(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(
                str(Path(directory) / "workflow.json"), ["spec_agent"]
            )
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
            workflow = DynamicWorkflowStore(
                str(Path(directory) / "workflow.json"), ["spec_agent"]
            )
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

    def test_stage_update_preserves_the_workflow_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            workflow = DynamicWorkflowStore(
                str(Path(directory) / "workflow.json"),
                ["spec_agent", "radio_design_agent"],
            )
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
        self.assertEqual(result.stages[1].tasks[0].target_agent, "radio_design_agent")
        self.assertEqual(result.stages[1].tasks[0].inputs, {"protocol": "BLE"})

    def test_registry_tool_persists_shared_state_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            state = SharedState(session_id="runtime-test")
            state.intent = SharedIntent.new("Build a BLE transmitter", "wf-test")
            ctx = ToolContext(extra={"state": state, "state_path": path})
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
            loaded = SharedState.load(path)

        self.assertTrue(result["ok"])
        self.assertEqual(loaded.intent.specification.field("goal").status, "aligned")

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
            workflow = DynamicWorkflowStore(
                str(Path(directory) / "workflow.json"), ["spec_agent"]
            )
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
