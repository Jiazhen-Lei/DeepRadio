import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grc.agent.service import MainAgentRuntime, build_mainagent_runtime
from grc.agent.service import session_store
from grc.agent.service.tools_lc import _call_registry
from grc.agent.state import SharedIntent, SharedState
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow.dynamic import DynamicWorkflowStore


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


class MainAgentRuntimeTest(unittest.TestCase):
    def test_turn_keeps_the_agent_reply_contract(self):
        class FakeMainAgent:
            def invoke(self, _input, _config):
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


if __name__ == "__main__":
    unittest.main()
