"""Regression checks for failures observed in the 0824_V3 hardware session."""

from __future__ import annotations

import os
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grc.agent import env
from grc.agent.service.stage_executor import bind_invocation_result
from grc.agent.service import session_store
from grc.agent.service.adapter import ServiceAgent
from grc.agent.state import SharedState
from grc.agent.tools import registry
from grc.agent.tools.hardware_profiles import resolve_hardware_profile
from grc.agent.tools.hardware_profiles import output_indicates_successful_probe
from grc.agent.tools.registry import ToolContext


class V3HardwareWorkflowRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = SharedState(session_id="v3-regression")
        self.ctx = ToolContext(platform=env.make_platform(), out_dir=str(self.root))
        self.ctx.extra["state"] = self.state
        registry.load_all()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_pluto_probe_is_bound_to_discovered_identity(self) -> None:
        profile = resolve_hardware_profile("plutosdr")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.command(probe=True, identity="usb:3.7.2"), [
            "iio_info", "-u", "usb:3.7.2",
        ])
        self.assertEqual(profile.command(probe=True), [])

    def test_iio_probe_accepts_device_enumeration_without_product_name(self) -> None:
        profile = resolve_hardware_profile("pluto")
        self.assertIsNotNone(profile)
        output = (
            "IIO context has 5 devices:\n"
            "\tiio:device0: ad9361-phy\n"
            "\tiio:device3: cf-ad9361-dds-core-lpc (buffer capable)\n"
        )
        self.assertNotIn("pluto", output.lower())
        self.assertTrue(output_indicates_successful_probe(profile, output))
        self.assertFalse(output_indicates_successful_probe(profile, ""))
        self.assertFalse(output_indicates_successful_probe(
            profile, "Unable to create IIO context"
        ))

    def test_preflight_requires_driver_and_reports_rf_enable_truthfully(self) -> None:
        self.state.project.config["device"] = {
            "type": "pluto",
            "center_freq": 2.402e9,
            "sample_rate": 2e6,
        }
        with mock.patch("grc.agent.tools.state_tools.shutil.which", return_value=None):
            missing = registry.call("hardware_preflight", {}, self.ctx)
        self.assertFalse(missing["ok"])
        self.assertIn("driver_command_available", missing["missing"])

        with mock.patch("grc.agent.tools.state_tools.shutil.which", return_value="/bin/iio_info"), \
                mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            ready = registry.call("hardware_preflight", {}, self.ctx)
        self.assertTrue(ready["ok"])
        self.assertTrue(ready["checks"]["real_hardware_actions_enabled"])

    def test_subagent_result_contract_rejects_semantic_shape_errors(self) -> None:
        invocation = {
            "task_id": "invocation-1",
            "workflow_id": "workflow-1",
            "stage_id": "stage-1",
            "workflow_revision": 1,
            "base_project_version": 0,
        }
        malformed = {
            **invocation,
            "ok": False,
            "outcome": "blocked",
            "artifacts": [],
            "completion": {"checked": "yes"},
        }
        bound = bind_invocation_result(dict(invocation), malformed)
        self.assertFalse(bound["protocol_valid"])

    def test_export_manifest_matches_relocatable_export(self) -> None:
        session_root = self.root / "sessions"
        export_root = self.root / "export"
        final = session_root / "export-test" / "final"
        (final / "ble").mkdir(parents=True)
        waveform = final / "ble" / "signal.cfile"
        waveform.write_bytes(b"signal")
        grc = final / "radio.grc"
        grc.write_text(f"file: '{waveform}'\n", encoding="utf-8")
        with mock.patch(
            "grc.agent.service.session_store.sessions_root",
            return_value=str(session_root),
        ):
            session_store.export_artifact(str(waveform), str(export_root))
            session_store.export_artifact(str(grc), str(export_root))
            session_store.rewrite_exported_grc_paths("export-test", str(export_root))
            manifest_path = session_store.write_export_manifest(
                "export-test", str(export_root)
            )
        exported_text = (export_root / "radio.grc").read_text(encoding="utf-8")
        self.assertIn("ble/signal.cfile", exported_text)
        self.assertNotIn(str(self.root), exported_text)
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["path_base"], "export_root")
        for item in manifest["artifacts"]:
            self.assertTrue((export_root / item["path"]).is_file())

    def test_ota_approval_is_bound_to_active_run_and_observed_name(self) -> None:
        sessions = self.root / "ota-sessions"
        with mock.patch(
            "grc.agent.service.session_store.sessions_root",
            return_value=str(sessions),
        ):
            agent = ServiceAgent(session_id="ota-contract")
            workflow = agent._workflow.consume_turn(
                "用 PlutoSDR 发射 BLE，local name 为 variable-name，直接部署",
                agent._state,
            )
            stage = agent._workflow.ensure_stage("over_air_verification")
            self.assertIsNotNone(stage)
            for prior in workflow.stages:
                if prior.id == stage.id:
                    break
                prior.execution_status = "completed"
                prior.outcome = "passed"
            workflow.current_stage = stage.id
            workflow.execution_status = "running"
            stage.execution_status = "running"
            checkpoint = agent._workflow.wait_for_checkpoint("LightBlue 空口验收")
            checkpoint.resume_stage = False
            now = time.time()
            running = {
                "ok": True,
                "running": True,
                "ready": True,
                "run_id": "run-variable",
                "deadline": now + 20,
            }
            stopped = {
                "ok": True,
                "running": False,
                "ready": False,
                "run_id": "run-variable",
                "reason": "stopped",
                "return_code": -15,
                "crashed": False,
            }

            def tool_result(name, _args, _ctx):
                return running if name == "query_runtime_status" else stopped

            with mock.patch(
                "grc.agent.tools.registry.call", side_effect=tool_result,
            ), mock.patch(
                "grc.agent.service.orchestrator.build_agent", return_value=None,
            ):
                reply = agent.step_command({
                    "action": "checkpoint_decision",
                    "checkpoint_id": checkpoint.id,
                    "decision": "approved",
                    "observation": {
                        "observed_name": "variable-name",
                        "observed_at": now,
                    },
                })
            observation = workflow.intent.slots["ota_observation"]
            self.assertEqual(observation["run_id"], "run-variable")
            self.assertEqual(observation["observed_name"], "variable-name")
            self.assertTrue(reply.done, reply.workflow_digest)

    def test_ota_approval_rejects_inactive_runtime(self) -> None:
        sessions = self.root / "expired-sessions"
        with mock.patch(
            "grc.agent.service.session_store.sessions_root",
            return_value=str(sessions),
        ):
            agent = ServiceAgent(session_id="ota-expired")
            workflow = agent._workflow.consume_turn(
                "用 PlutoSDR 发射 BLE，local name 为 expired-name，直接部署",
                agent._state,
            )
            stage = agent._workflow.ensure_stage("over_air_verification")
            for prior in workflow.stages:
                if prior.id == stage.id:
                    break
                prior.execution_status = "completed"
                prior.outcome = "passed"
            workflow.current_stage = stage.id
            workflow.execution_status = "running"
            stage.execution_status = "running"
            checkpoint = agent._workflow.wait_for_checkpoint("LightBlue 空口验收")
            checkpoint.resume_stage = False
            with mock.patch(
                "grc.agent.tools.registry.call",
                return_value={
                    "ok": True,
                    "running": False,
                    "ready": False,
                    "run_id": "run-expired",
                    "reason": "stopped",
                    "return_code": -15,
                    "crashed": False,
                },
            ), mock.patch(
                "grc.agent.service.orchestrator.build_agent", return_value=None,
            ):
                reply = agent.step_command({
                    "action": "checkpoint_decision",
                    "checkpoint_id": checkpoint.id,
                    "decision": "approved",
                    "observation": {
                        "observed_name": "expired-name",
                        "observed_at": time.time(),
                    },
                })
            self.assertEqual(reply.stage, "CRITIC")
            self.assertIn("not within a valid runtime window", reply.text)
            self.assertEqual(stage.execution_status, "waiting")

    def test_unarmed_tx_preview_is_valid_and_compiles(self) -> None:
        if "iio_pluto_sink" not in self.ctx.platform.blocks:
            self.skipTest("iio_pluto_sink is not available in this GNU Radio build")
        built = registry.call(
            "build_sdr_tx_flowgraph",
            {
                "device_type": "pluto",
                "center_freq": 2.402e9,
                "sample_rate": 2e6,
            },
            self.ctx,
        )
        self.assertTrue(built.get("ok"), built)
        self.assertTrue(built.get("valid"), built)
        self.assertTrue(built.get("compiled"), built)
        self.assertTrue(Path(built["report_path"]).is_file())
        naive = registry.call("validate_flowgraph", {}, self.ctx)
        self.assertTrue(naive.get("valid"), naive)
        armed = registry.call(
            "validate_flowgraph", {"arm_disabled_rf": True}, self.ctx
        )
        self.assertTrue(armed.get("valid"), armed)
        sink = self.ctx.blocks.get("sdr_sink")
        self.assertEqual(getattr(sink, "state", None), "disabled")
        text = Path(built["grc_path"]).read_text(encoding="utf-8")
        self.assertTrue("blocks_throttle2" in text or "blocks_throttle" in text)
        self.assertIn("Unauthorized RF", self.ctx.blocks["sdr_sink"].comment)
        self.assertIn("1 kHz", self.ctx.blocks["src"].comment)
        self.assertIn("Rate limiting only", self.ctx.blocks["preview_throttle"].comment)
        self.assertIn("Safe preview", self.ctx.blocks["preview_sink"].comment)
        self.assertEqual(
            getattr(self.ctx.blocks.get("preview_throttle"), "state", None),
            "enabled",
        )
        self.assertEqual(
            getattr(self.ctx.blocks.get("preview_sink"), "state", None),
            "enabled",
        )

    def test_preview_bind_writes_identity_without_arming(self) -> None:
        if "iio_pluto_sink" not in self.ctx.platform.blocks:
            self.skipTest("iio_pluto_sink is not available in this GNU Radio build")
        from grc.agent.tools.hardware_tools import bind_endpoint_identity

        built = registry.call(
            "build_sdr_tx_flowgraph",
            {"device_type": "pluto", "center_freq": 2.402e9, "sample_rate": 2e6},
            self.ctx,
        )
        changed = bind_endpoint_identity(self.ctx.flow_graph, "usb:2.4.5")
        self.assertGreater(changed, 0)
        self.ctx.platform.save_flow_graph(built["grc_path"], self.ctx.flow_graph)
        text = Path(built["grc_path"]).read_text(encoding="utf-8")
        self.assertIn("usb:2.4.5", text)
        self.assertEqual(self.ctx.blocks["sdr_sink"].state, "disabled")

    def test_generic_tx_arm_binds_identity_and_disables_preview(self) -> None:
        if "iio_pluto_sink" not in self.ctx.platform.blocks:
            self.skipTest("iio_pluto_sink is not available in this GNU Radio build")
        built = registry.call(
            "build_sdr_tx_flowgraph",
            {"device_type": "pluto", "center_freq": 2.402e9, "sample_rate": 2e6},
            self.ctx,
        )
        self.ctx.extra["workflow"] = {
            "intent": {"slots": {"direction": "tx"}},
            "stages": [
                {
                    "id": "discover_and_probe_hardware",
                    "execution_status": "completed",
                    "outcome": "passed",
                    "result": {"completion": {"device_probed": True}},
                },
                {
                    "id": "rf_plan_confirmation",
                    "checkpoint": {
                        "decision_status": "approved",
                        "requested_effect": "RF_RUN",
                    },
                },
            ],
        }
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            armed = registry.call(
                "arm_hardware_flowgraph",
                {
                    "grc_path": built["grc_path"],
                    "device_identity": "usb:test.identity",
                },
                self.ctx,
            )
        self.assertTrue(armed.get("ok"), armed)
        self.assertTrue(armed.get("compile", {}).get("compiled"), armed)
        self.assertTrue(Path(armed["grc_path"]).is_file())
        text = Path(armed["grc_path"]).read_text(encoding="utf-8")
        self.assertIn("usb:test.identity", text)
        self.assertEqual(self.ctx.blocks["sdr_sink"].state, "enabled")
        self.assertEqual(self.ctx.blocks["preview_throttle"].state, "disabled")
        self.assertEqual(self.ctx.blocks["preview_sink"].state, "disabled")


# --- test_v6_followup_contracts.py ---

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grc.agent.service import session_store as store
from grc.agent.state import SharedState
from grc.agent.tools import registry
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow import WorkflowEngine
from grc.agent.workflow.completion import evaluate
from grc.agent.schema import AgentReply, ToolInvocation


class V6FollowupContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self._sessions_patch = mock.patch(
            "grc.agent.service.session_store.sessions_root",
            return_value=str(self.sessions),
        )
        self._sessions_patch.start()

    def tearDown(self):
        self._sessions_patch.stop()
        self.temp.cleanup()

    def test_export_manifest_uses_explicit_file_list(self):
        session_id = "gui-v6"
        final = Path(store.session_root(session_id)) / "final"
        final.mkdir(parents=True)
        artifact = final / "ble.json"
        artifact.write_text('{"local_name":"loveu"}', encoding="utf-8")
        export_root = self.root / "output"
        sibling = export_root / "0824_V5"
        sibling.mkdir(parents=True)
        (sibling / "old.txt").write_text("stale", encoding="utf-8")
        (export_root / "__pycache__").mkdir()
        dest = Path(store.nested_export_dir(session_id, str(export_root)))
        copied = store.export_artifact(str(artifact), str(dest))
        store.write_export_manifest(session_id, str(dest), [copied])
        payload = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
        paths = [item["path"] for item in payload["artifacts"]]
        self.assertEqual(paths, [os.path.relpath(copied, dest)])
        self.assertTrue(all("sha256" in item and item["size"] > 0 for item in payload["artifacts"]))
        self.assertNotIn("0824_V5", " ".join(paths))
        self.assertNotIn("__pycache__", " ".join(paths))

    def test_session_manifest_is_cumulative_and_roles_are_semantic(self):
        from grc.agent.service import result_projector

        session_id = "artifact-index"
        state = SharedState(session_id=session_id)
        state.runtime.current_node = "probe_device"
        final = Path(store.session_root(session_id)) / "final"
        reports = final / "reports"
        reports.mkdir(parents=True)
        device_report = reports / "device_probe.json"
        device_report.write_text('{"identity_ok":true}', encoding="utf-8")
        first_manifest = store.write_artifact_manifest(
            session_id, {"device_report": str(device_report)}
        )
        result_projector.project_artifact_index(state, first_manifest)

        state.runtime.current_node = "prepare_artifact"
        preview = final / "radio.grc"
        preview.write_text("options:\n  parameters: {}\n", encoding="utf-8")
        manifest = store.write_artifact_manifest(
            session_id, {"grc_path": str(preview)}
        )
        payload = json.loads(Path(manifest).read_text(encoding="utf-8"))
        result_projector.project_artifact_index(state, manifest)
        entries = {item["path"]: item for item in payload["artifacts"]}
        self.assertIn("final/reports/device_probe.json", entries)
        self.assertIn("final/radio.grc", entries)
        self.assertEqual(
            entries["final/reports/device_probe.json"]["role"],
            "device_report",
        )
        self.assertEqual(entries["final/radio.grc"]["role"], "safe_preview")
        self.assertTrue(all(item.get("artifact_id") for item in entries.values()))
        projected = {item.path: item for item in state.artifacts}
        self.assertEqual(
            projected["final/reports/device_probe.json"].producer,
            "probe_device",
        )
        self.assertEqual(projected["final/radio.grc"].producer, "prepare_artifact")

    def test_attach_evidence_binds_run_id_and_hash(self):
        session_id = "gui-evidence"
        source = self.root / "lightblue.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake")
        attached = store.attach_evidence(
            session_id, str(source), run_id="run-abc123"
        )
        self.assertTrue(attached["sha256"])
        self.assertIn("final/evidence/run-abc123/", attached["path"])
        self.assertTrue(Path(attached["artifact"]).is_file())

    def test_checkpoint_writes_completion_result(self):
        engine = WorkflowEngine(str(self.root / "workflow.yaml"))
        workflow = engine.consume_turn(
            "用 plutosdr 发射 ble 信号，local name 为 loveu",
            SharedState(),
        )
        workflow.current_stage = "rf_plan_confirmation"
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            engine._activate_current()
            engine.resolve_checkpoint("approved")
        stage = workflow.stage("rf_plan_confirmation")
        self.assertEqual(stage.outcome, "passed")
        self.assertTrue(stage.result["completion"]["rf_plan_approved"])
        digest = engine.digest()
        rf = next(item for item in digest["stages"] if item["id"] == "rf_plan_confirmation")
        self.assertTrue(rf["completion_result"]["rf_plan_approved"])

    def test_rf_capability_blocker_clears_only_after_runtime_restart(self):
        workflow_path = self.root / "workflow.yaml"
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "0"}):
            engine = WorkflowEngine(str(workflow_path))
            workflow = engine.consume_turn(
                "用 plutosdr 发射 ble 信号，local name 为 loveu",
                SharedState(),
            )
            workflow.current_stage = "rf_plan_confirmation"
            engine._activate_current()
            engine.save()
            digest = engine.digest()
            self.assertEqual(digest["wait_kind"], "capability")
            self.assertEqual(
                digest["blocker"]["code"], "SYSTEM_CAPABILITY_MISSING"
            )
            self.assertEqual(digest["blocker"]["requested_effect"], "RF_RUN")
            with self.assertRaises(ValueError):
                engine.resolve_checkpoint("approved")

        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}):
            restored = WorkflowEngine(str(workflow_path))
            digest = restored.digest()
            self.assertEqual(digest["wait_kind"], "approval")
            self.assertFalse(digest["blocker"])
            restored.resolve_checkpoint("approved")

    def test_ota_checkpoint_records_run_id_and_evidence(self):
        engine = WorkflowEngine(str(self.root / "workflow.yaml"))
        workflow = engine.consume_turn(
            "用 plutosdr 发射 ble 信号，local name 为 loveu",
            SharedState(),
        )
        workflow.intent.slots["over_air_observed"] = True
        workflow.intent.slots["ota_observation"] = {
            "run_id": "run-f646528e87c5",
            "artifact": "final/evidence/run-f646528e87c5/shot.png",
            "sha256": "abc",
        }
        self.assertIsNotNone(engine.ensure_stage("over_air_verification"))
        workflow.current_stage = "over_air_verification"
        engine._activate_current()
        engine.resolve_checkpoint("approved")
        stage = workflow.stage("over_air_verification")
        self.assertTrue(stage.result["completion"]["over_air_observed"])
        self.assertEqual(stage.result["acceptance"]["run_id"], "run-f646528e87c5")
        self.assertEqual(
            stage.result["acceptance"]["evidence_id"],
            "final/evidence/run-f646528e87c5/shot.png",
        )

    def test_ble_spec_digest_is_not_generic_question_marks(self):
        state = SharedState(session_id="spec")
        state.project.config.update({
            "protocol": "ble",
            "hardware": "pluto",
            "local_name": "loveu",
            "ble_channel": 37,
            "carrier_frequency": 2_402_000_000.0,
            "max_duration_seconds": 30.0,
            "modulation": "gfsk",
        })
        digest = state.spec_digest()
        self.assertEqual(digest["spec_kind"], "ble")
        self.assertIn("BLE 1M", digest["summary"])
        self.assertIn("CH37", digest["summary"])
        self.assertIn("2.402 GHz", digest["summary"])
        self.assertIn("PlutoSDR", digest["summary"])
        self.assertIn("Local Name=loveu", digest["summary"])
        self.assertNotIn("GFSK → ? → ?", digest["summary"])
        self.assertIn("最大时长", digest["duration_note"])

    def test_hardware_spec_digest_is_not_link_placeholders(self):
        state = SharedState(session_id="hw-spec")
        state.project.config.update({
            "hardware": "pluto",
            "direction": "tx",
            "carrier_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "rf_armed": False,
        })
        digest = state.spec_digest()
        self.assertIn("PlutoSDR", digest["summary"])
        self.assertIn("2.402 GHz", digest["summary"])
        self.assertIn("sink unarmed", digest["summary"])
        self.assertNotIn("? → ? → ?", digest["summary"])

    def test_configure_sdr_uses_configuration_mode(self):
        registry.load_all()
        state = SharedState(session_id="cfg")
        ctx = ToolContext(platform=None, out_dir=str(self.root))
        ctx.extra["state"] = state
        ctx.extra["workflow"] = {
            "stages": [{
                "id": "rf_plan_confirmation",
                "checkpoint": {"decision_status": "approved"},
            }]
        }
        result = registry.call(
            "configure_sdr",
            {
                "device_type": "pluto",
                "center_freq": 2.402e9,
                "sample_rate": 2e6,
            },
            ctx,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["device"]["configuration_mode"], "recorded")
        self.assertEqual(result["device"]["mode"], "configuration_recorded")
        self.assertNotEqual(result["device"]["mode"], "flowgraph_config_only")
        envelope_stage = mock.Mock(completion=["hardware_check_completed"])
        workflow = mock.Mock()
        workflow.intent = mock.Mock(slots={}, capabilities=[])
        workflow.stages = []
        checks = evaluate(envelope_stage, workflow, state, AgentReply())
        self.assertFalse(checks["hardware_check_completed"])
        reply = AgentReply(tool_invocations=[ToolInvocation(
            name="hardware_preflight",
            result={"ok": True, "missing": []},
            ok=True,
        )])
        workflow.intent.slots = {
            "hardware": "pluto",
            "carrier_frequency": 2.402e9,
            "sample_rate": 2e6,
        }
        checks = evaluate(envelope_stage, workflow, state, reply)
        self.assertTrue(checks["hardware_check_completed"])

    def test_session_event_sequences_are_unique_under_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor

        session_id = "event-seq"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(
                lambda index: store.append_session_event(
                    session_id, "parallel", {"index": index}
                ),
                range(64),
            ))
        records = [
            json.loads(line)
            for line in (
                Path(store.session_root(session_id)) / "events.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        sequences = [int(item["seq"]) for item in records]
        self.assertEqual(sequences, list(range(1, 65)))

    def test_deepagent_thread_is_isolated_per_stage_taskcard(self):
        class FakeAgent:
            def __init__(self):
                self.thread_ids = []

            def invoke(self, _payload, config):
                self.thread_ids.append(config["configurable"]["thread_id"])
                return {"messages": [{"content": "ok"}]}

        service = ServiceAgent(session_id="deep-stage-thread", platform=None)
        ctx = ToolContext(platform=None, out_dir=str(self.root / "deep"))
        ctx.extra.update({
            "state": service._state,
            "artifacts": {},
            "events": [],
            "metrics": {},
            "workflow": {"workflow_id": "wf-one", "revision": 3},
            "stage_id": "hardware_precheck",
            "task_card": {"task_id": "task-a"},
        })
        fake = FakeAgent()
        service._run_deep(fake, ctx, "first")
        ctx.extra["stage_id"] = "configure_and_check"
        ctx.extra["task_card"] = {"task_id": "task-b"}
        service._run_deep(fake, ctx, "second")
        self.assertEqual(len(set(fake.thread_ids)), 2)
        self.assertIn("hardware_precheck", fake.thread_ids[0])
        self.assertIn("configure_and_check", fake.thread_ids[1])

    def test_state_save_uses_relative_session_paths(self):
        session_id = "gui-rel"
        root = Path(store.session_root(session_id))
        final = root / "final"
        final.mkdir(parents=True)
        grc = final / "radio.grc"
        grc.write_text("options: {}\n", encoding="utf-8")
        state = SharedState(session_id=session_id)
        state.project.grc_path = str(grc)
        state.project.config["runtime"] = {
            "program": str(final / "hardware_runtime" / "ble.py"),
            "log_path": str(final / "hardware_runtime" / "runtime.log"),
            "status": "stopped",
        }
        saved = store.state_path(session_id)
        state.save(saved)
        payload = json.loads(Path(saved).read_text(encoding="utf-8"))
        self.assertFalse(os.path.isabs(payload["project"]["grc_path"]))
        self.assertTrue(payload["project"]["grc_path"].startswith("final/"))
        loaded = SharedState.load(saved, session_id=session_id)
        self.assertTrue(os.path.isabs(loaded.project.grc_path))
        self.assertEqual(Path(loaded.project.grc_path), grc)

    def test_archive_session_rewrites_moved_paths(self):
        session_id = "gui-archive"
        root = Path(store.session_root(session_id))
        final = root / "final"
        final.mkdir(parents=True)
        grc = final / "radio.grc"
        grc.write_text("options: {}\n", encoding="utf-8")
        state = SharedState(session_id=session_id)
        state.project.grc_path = str(grc)
        state.save(store.state_path(session_id))
        dest = self.root / "archived" / session_id
        archived = store.archive_session(session_id, str(dest))
        payload = json.loads((Path(archived) / "state.json").read_text(encoding="utf-8"))
        self.assertFalse(os.path.isabs(payload["project"]["grc_path"]))
        loaded = SharedState.load(str(Path(archived) / "state.json"), session_id=session_id)
        self.assertTrue(Path(loaded.project.grc_path).is_file())


    def test_deterministic_handler_events_are_not_called_subagent(self):
        from grc.agent import env
        from grc.agent.service.adapter import ServiceAgent

        try:
            platform = env.make_platform()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"GNU Radio platform unavailable: {exc}")
        if "iio_pluto_sink" not in platform.blocks:
            self.skipTest("gr-iio Pluto sink not installed")
        with mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id="ble-events")
            reply = agent.step(
                "用plutosdr发射一段2.402GHz的ble信号，local name为loveu，"
                "发射30秒，成功条件为LightBlue观察到loveu"
            )
        events = (self.sessions / "ble-events" / "events.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn('"event": "stage_routed"', events)
        self.assertIn('"event": "deterministic_handler_started"', events)
        self.assertNotIn('"event": "subagent_invoked"', events)
        self.assertIn("BLE 1M", reply.spec_digest.get("summary") or "")
        self.assertIn("loveu", reply.spec_digest.get("summary") or "")
        self.assertIn("BLE PDU generated", reply.text or "")

    def test_gui_emergency_stop_command_revokes_rf_grant(self):
        from grc.agent.service.adapter import ServiceAgent

        with mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ), mock.patch(
            "grc.agent.tools.registry.call",
            return_value={
                "ok": True, "run_id": "run-ui", "running": False,
                "reason": "emergency_stop", "return_code": -9,
            },
        ) as called:
            agent = ServiceAgent(session_id="gui-emergency-stop")
            agent._state.runtime.granted_effects = ["READ", "RF_RUN"]
            reply = agent.step_command({"action": "emergency_stop"})
        called.assert_called_once_with("emergency_stop", {}, mock.ANY)
        self.assertEqual(reply.stage, "RUNTIME")
        self.assertIn("Emergency stop", reply.text)
        self.assertNotIn("RF_RUN", agent._state.runtime.granted_effects)


# --- test_tool_origin_and_profiles.py ---

"""Contracts for tool origin labels and HardwareProfile device args."""


class FriendlyFailureAndRetryContractTest(unittest.TestCase):
    """V4 GUI contracts: friendly failure text and hardware-aware retry."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.temp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self.temp.cleanup()

    def test_friendly_stage_failure_hides_internal_codes(self):
        from grc.agent.service.adapter import _friendly_stage_failure

        text = _friendly_stage_failure(
            ["hardware_endpoint_present"],
            ["REPLY_STATUS_REJECTED", "MISSING_COMPLETION:flowgraph_saved"],
        )
        self.assertNotIn("MISSING_COMPLETION", text)
        self.assertNotIn("REPLY_STATUS_REJECTED", text)
        self.assertNotIn("hardware_endpoint_present", text)
        self.assertIn("did not finish", text)
        self.assertIn("SDR hardware output", text)
        self.assertIn("retry", text.lower())
        self.assertIn("diagnose", text.lower())

    def _waiting_agent(self, session_id):
        from grc.agent.service.adapter import ServiceAgent

        with mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id=session_id)
            agent.step(
                "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，"
                "保存配置并停在发射确认。"
            )
        return agent

    def test_retry_reports_missing_device_and_does_not_rerun(self):
        agent = self._waiting_agent("retry-no-device")
        workflow = agent._workflow.workflow
        self.assertEqual(workflow.execution_status, "waiting")
        stage_before = agent._workflow.current_stage()
        with mock.patch(
            "grc.agent.tools.registry.call",
            return_value={"ok": True, "device_found": False, "devices": []},
        ) as called:
            reply = agent.step_command({"action": "retry_stage"})
        self.assertEqual(
            called.call_args_list[0].args[0], "discover_devices"
        )
        self.assertEqual(reply.stage, "WAITING")
        self.assertIn("No SDR was detected", reply.text)
        self.assertEqual(
            agent._workflow.current_stage().id, stage_before.id
        )
        self.assertEqual(
            agent._workflow.workflow.execution_status, "waiting"
        )

    def test_retry_probe_evidence_survives_into_next_stage_context(self):
        agent = self._waiting_agent("retry-with-device")

        def side_effect(name, args, ctx):
            if name == "discover_devices":
                return {
                    "ok": True,
                    "device_found": True,
                    "device_type": "pluto",
                    "devices": [
                        {"device_type": "pluto", "device_identity": "usb:0"}
                    ],
                }
            if name == "probe_device":
                return {
                    "ok": True,
                    "device_probed": True,
                    "device_type": "pluto",
                    "device_identity": "usb:0",
                }
            return {"ok": True}

        with mock.patch(
            "grc.agent.tools.registry.call", side_effect=side_effect
        ):
            note = agent._refresh_hardware_for_retry()
        self.assertIn("Re-checked the SDR", note)
        self.assertIn("usb:0", note)
        self.assertTrue(agent._retry_preflight_events)
        kinds = {
            item.get("kind") for item in agent._retry_preflight_events
        }
        self.assertIn("discover_devices", kinds)
        self.assertIn("probe_device", kinds)
        # The next stage context must inherit the fresh evidence.
        ctx = agent._make_ctx()
        event_kinds = {item.get("kind") for item in ctx.extra["events"]}
        self.assertIn("probe_device", event_kinds)
        self.assertEqual(agent._retry_preflight_events, [])

    def test_inspect_plan_without_patch_is_honest(self):
        """V4 regression: no false 'change plan was created' claim."""
        from grc.agent.service.stage_handlers import _handle_inspect_plan

        agent = self._waiting_agent("inspect-honest")
        ctx = agent._make_ctx()
        with mock.patch(
            "grc.agent.tools.registry.call",
            return_value={"ok": True, "path": "/tmp/current.grc"},
        ):
            reply = _handle_inspect_plan(agent, ctx, "", "", True)
        self.assertNotIn("change plan was created", reply.text)
        self.assertIn("could not derive a concrete change plan", reply.text)
        self.assertIn("nothing was modified", reply.text)


import unittest

from grc.agent.service.session_store import _event_actor
from grc.agent.tools.hardware_profiles import device_args_for
from grc.agent.tools import registry


class DeviceArgsFromProfileTests(unittest.TestCase):
    def test_b210_uses_profile_default(self):
        self.assertEqual(device_args_for("b210"), "type=b200")
        self.assertEqual(device_args_for("b200"), "type=b200")

    def test_pluto_has_no_uhd_args(self):
        self.assertEqual(device_args_for("pluto"), "")

    def test_override_wins(self):
        self.assertEqual(device_args_for("b210", "serial=xyz"), "serial=xyz")


class ToolOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registry.load_all()

    def test_ble_protocol_tools_are_deepradio(self):
        self.assertEqual(
            registry.origin_of("build_ble_advertising_pdu"), "deepradio_protocol"
        )
        self.assertEqual(
            registry.origin_of("generate_ble_1m_waveform"), "deepradio_protocol"
        )
        self.assertEqual(
            registry.origin_of("verify_ble_packet_bits"), "deepradio_protocol"
        )

    def test_flowgraph_builders_compose_gnuradio(self):
        self.assertEqual(
            registry.origin_of("build_ble_pluto_tx_flowgraph"), "deepradio_compose"
        )
        self.assertEqual(
            registry.origin_of("build_ble_uhd_tx_flowgraph"), "deepradio_compose"
        )
        self.assertEqual(registry.runtime_of("add_block"), "gnuradio_blocks")

    def test_discover_and_runtime_origins(self):
        self.assertEqual(registry.origin_of("discover_devices"), "vendor_cli")
        self.assertEqual(registry.origin_of("probe_device"), "vendor_cli")
        self.assertEqual(registry.origin_of("arm_hardware_flowgraph"), "deepradio_runtime")
        self.assertEqual(registry.origin_of("start_flowgraph"), "deepradio_runtime")

    def test_action_registry_exposes_generic_effect_contracts(self):
        expected = {
            "inspect_flowgraph": "READ",
            "build_sdr_tx_flowgraph": "ARTIFACT_WRITE",
            "discover_devices": "DEVICE_READ",
            "arm_hardware_flowgraph": "DEVICE_CONFIG",
            "start_flowgraph": "RF_RUN",
        }
        for name, effect in expected.items():
            metadata = registry.action_metadata(name)
            self.assertEqual(metadata.get("effect_level"), effect, metadata)
        self.assertFalse(registry.action_metadata("start_flowgraph")["idempotent"])
        self.assertIn(
            "user_effect_grant",
            registry.action_metadata("start_flowgraph")["requires"],
        )

    def test_timeline_actor_includes_origin(self):
        actor = _event_actor(
            {
                "tool": "build_ble_advertising_pdu",
                "origin": "deepradio_protocol",
                "mode": "deterministic",
            }
        )
        self.assertIn("deepradio_protocol", actor)
        self.assertIn("build_ble_advertising_pdu", actor)


# --- test_usrp_rx_spectrum_contracts.py ---

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grc.agent import env
from grc.agent.service import session_store as store
from grc.agent.service.adapter import ServiceAgent
from grc.agent.state import SharedState
from grc.agent.tools import registry
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow import WorkflowEngine


class UsrpRxSpectrumContractTest(unittest.TestCase):
    def setUp(self):
        try:
            self.platform = env.make_platform()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"make_platform unavailable: {exc}")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        registry.load_all()
        self.ctx = ToolContext(platform=self.platform, out_dir=str(self.root))
        self.ctx.extra["state"] = SharedState(session_id="rx-spectrum")

    def tearDown(self):
        self.temp.cleanup()

    def test_intent_skips_alignment_and_selects_runtime_stages(self):
        engine = WorkflowEngine(str(self.root / "workflow.yaml"))
        workflow = engine.consume_turn(
            "使用usrpb210构建接收机，在2.402GHz绘制出实时的频谱图",
            SharedState(),
        )
        self.assertEqual(workflow.task_type, "RX_BUILD")
        self.assertEqual(workflow.intent.slots["hardware"], "b210")
        self.assertEqual(workflow.intent.slots["carrier_frequency"], 2_402_000_000.0)
        self.assertEqual(workflow.intent.slots["sample_rate"], 2_000_000.0)
        self.assertEqual(workflow.intent.slot_sources["sample_rate"], "default")
        self.assertEqual(workflow.intent.missing_slots, [])
        self.assertEqual(workflow.current_stage, "rx_build_and_verify")
        self.assertIn("realtime_sink_present", workflow.stage("rx_build_and_verify").completion)
        self.assertEqual(workflow.stages[-1].id, "rf_plan_confirmation")
        self.assertIn(
            "stop_runtime",
            [item.get("id") for item in workflow.deferred_plan],
        )

    def test_flowgraph_has_usrp_source_and_qt_spectrum(self):
        built = registry.call(
            "build_usrp_rx_spectrum_flowgraph",
            {"center_freq": 2.402e9, "sample_rate": 2e6},
            self.ctx,
        )
        self.assertTrue(built["ok"], built)
        self.assertTrue(built["not_started"])
        path = Path(built["grc_path"])
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("uhd_usrp_source", text)
        self.assertIn("qtgui_freq_sink_x", text)
        self.assertIn("2402000000", text.replace(" ", ""))

    def test_generic_rx_builder_supports_pluto_without_starting(self):
        built = registry.call(
            "build_sdr_rx_spectrum_flowgraph",
            {
                "device_type": "pluto",
                "center_freq": 2.402e9,
                "sample_rate": 2e6,
                "device_args": "usb:test.pluto",
            },
            self.ctx,
        )
        self.assertTrue(built["ok"], built)
        self.assertTrue(built["not_started"])
        self.assertEqual(built["signal_source_scope"], "live_device")
        text = Path(built["grc_path"]).read_text(encoding="utf-8")
        self.assertIn("iio_pluto_source", text)
        self.assertIn("qtgui_freq_sink_x", text)

    def test_service_agent_builds_offline_and_does_not_start_rf(self):
        sessions = self.root / "sessions"
        with mock.patch.object(store, "sessions_root", return_value=str(sessions)), mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id="rx-spectrum-svc", platform=self.platform)
            reply = agent.step(
                "使用usrpb210构建接收机，在2.402GHz绘制出实时的频谱图"
            )
            events = Path(store.session_root("rx-spectrum-svc")) / "events.jsonl"
            event_text = events.read_text(encoding="utf-8")
        self.assertEqual(reply.workflow_digest["task_type"], "RX_BUILD")
        self.assertTrue(Path(reply.artifacts["grc_path"]).is_file())
        self.assertIn("uhd_usrp_source", Path(reply.artifacts["grc_path"]).read_text())
        self.assertEqual(
            reply.workflow_digest["current_stage"], "discover_and_probe_hardware"
        )
        self.assertEqual(reply.workflow_digest["wait_kind"], "recovery")
        self.assertTrue(reply.workflow_digest.get("timeline"))
        self.assertNotIn('"start_flowgraph"', event_text)


class B210HilGateTest(unittest.TestCase):
    def test_rf_and_hil_remain_opt_in(self):
        if __import__("os").environ.get("GRC_AGENT_ENABLE_RF") == "1":
            self.skipTest(
                "GRC_AGENT_ENABLE_RF=1 is set in this shell; unset it for Gate 1"
            )
        if __import__("os").environ.get("GRC_AGENT_HIL") != "1":
            self.skipTest("Set GRC_AGENT_HIL=1 with a connected B210 to run live discover/probe")
        try:
            platform = env.make_platform()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"make_platform unavailable: {exc}")
        ctx = ToolContext(platform=platform, out_dir=tempfile.mkdtemp())
        registry.load_all()
        discovered = registry.call("discover_devices", {"device_type": "b210"}, ctx)
        if not discovered.get("device_found"):
            self.skipTest("B210 not found: " + str(discovered.get("error") or discovered))
        probed = registry.call("probe_device", {"device_type": "b210"}, ctx)
        self.assertTrue(probed.get("device_probed"))


class RuntimeQualityProjectionTest(unittest.TestCase):
    def test_structured_stream_counters_raise_visible_warning(self):
        from grc.agent.schema import AgentReply, ToolInvocation
        from grc.agent.service.result_projector import project_tool_results

        state = SharedState(session_id="quality-projection")
        recorded = []

        def record_claim(*args, **kwargs):
            recorded.append((args, kwargs))

        reply = AgentReply(tool_invocations=[ToolInvocation(
            name="query_runtime_status",
            result={
                "ok": True,
                "running": False,
                "run_id": "run-quality",
                "reason": "exited",
                "return_code": 0,
                "crashed": False,
                "underrun_count": 4,
                "overrun_count": 1,
            },
        )])
        project_tool_results(
            state,
            reply,
            record_claim=record_claim,
            semantic_hash=lambda _path: "",
        )

        self.assertEqual(state.runtime.quality, "warning")
        self.assertEqual(state.runtime.warnings[0]["underrun_count"], 4)
        self.assertEqual(state.runtime.warnings[0]["overrun_count"], 1)
        self.assertTrue(any(
            args and args[0] == "rf_runtime_underflow" and args[5] is False
            for args, _kwargs in recorded
        ))

    def test_generated_script_reload_uses_current_source_bytes(self):
        from grc.agent.runtime.simulate import _load_top_block_class

        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "generated.py"
            first = (
                "class top_block:\n"
                "    marker = 1\n"
                "    def start(self): pass\n"
            )
            second = first.replace("marker = 1", "marker = 2")
            script.write_text(first, encoding="utf-8")
            original_times = (script.stat().st_atime_ns, script.stat().st_mtime_ns)
            self.assertEqual(_load_top_block_class(str(script)).marker, 1)

            script.write_text(second, encoding="utf-8")
            os.utime(script, ns=original_times)

            self.assertEqual(_load_top_block_class(str(script)).marker, 2)


# --- test_hardware_runtime_contracts.py ---

import sys
import tempfile
import time
import unittest
from pathlib import Path

from grc.agent.service.hardware_runtime import HardwareRuntime


class HardwareRuntimeContractTest(unittest.TestCase):
    def test_stream_quality_counts_scheduler_markers(self):
        quality = HardwareRuntime._stream_quality("UUU\nnormal output\nOO")
        self.assertEqual(quality["underrun_count"], 3)
        self.assertEqual(quality["overrun_count"], 2)
        self.assertTrue(quality["stream_quality_warning"])

    def test_windows_python_script_uses_current_interpreter(self):
        process = mock.Mock()
        process.pid = 123
        process.stdout = None
        process.poll.return_value = None
        windows_os = mock.Mock(wraps=os)
        windows_os.name = "nt"
        with mock.patch(
            "grc.agent.service.hardware_runtime.os", windows_os
        ), mock.patch(
            "grc.agent.service.hardware_runtime.subprocess.Popen",
            return_value=process,
        ) as popen:
            result = self.runtime.start("windows-script", "radio.py", 30)
        timer = self.runtime._processes["windows-script"].get("timer")
        if timer:
            timer.cancel()
        self.runtime._processes.pop("windows-script", None)
        self.assertEqual(result["interpreter"], sys.executable)
        command = popen.call_args.args[0]
        self.assertEqual(command[:2], [sys.executable, "-u"])
        self.assertFalse(popen.call_args.kwargs["start_new_session"])

    def test_windows_termination_uses_process_methods(self):
        process = mock.Mock()
        windows_os = mock.Mock(wraps=os)
        windows_os.name = "nt"
        with mock.patch("grc.agent.service.hardware_runtime.os", windows_os):
            HardwareRuntime._terminate_process(process, emergency=False)
            process.terminate.assert_called_once_with()
            HardwareRuntime._terminate_process(process, emergency=True)
            process.kill.assert_called_once_with()

    def test_relocatable_paths_use_forward_slashes(self):
        windows_path = mock.Mock(wraps=os.path)
        windows_path.relpath.return_value = r"final\evidence\capture.png"
        windows_os = mock.Mock(wraps=os)
        windows_os.sep = "\\"
        windows_os.path = windows_path
        with mock.patch.object(session_store, "os", windows_os):
            relative = session_store._posix_relpath("target", "root")
        self.assertEqual(relative, "final/evidence/capture.png")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = HardwareRuntime()

    def tearDown(self):
        for session_id in list(self.runtime._processes):
            self.runtime.stop(session_id, emergency=True)
        self.temp.cleanup()

    def program(self, name: str, body: str) -> str:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def wait_until_stopped(self, session_id: str, timeout: float = 2.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.runtime.status(session_id)
            if not result.get("running"):
                return result
            time.sleep(0.05)
        self.fail(f"runtime {session_id} did not stop before timeout")

    def test_regular_stop_terminates_process_group(self):
        program = self.program(
            "regular_stop.py", "import time\nprint('ready', flush=True)\ntime.sleep(30)"
        )
        started = self.runtime.start("regular", program, 60)
        self.assertTrue(started["running"])
        stopped = self.runtime.stop("regular")
        self.assertTrue(stopped["ok"])
        self.assertEqual(stopped["reason"], "stopped")
        self.assertFalse(stopped["running"])

    def test_emergency_stop_is_always_available(self):
        program = self.program(
            "emergency_stop.py", "import time\nprint('ready', flush=True)\ntime.sleep(30)"
        )
        self.runtime.start("emergency", program, 60)
        stopped = self.runtime.stop("emergency", emergency=True)
        self.assertTrue(stopped["ok"])
        self.assertTrue(stopped["emergency"])
        self.assertEqual(stopped["reason"], "emergency_stop")

    def test_nonzero_natural_exit_is_reported_as_crash(self):
        program = self.program(
            "crash.py", "import sys\nprint('boom', flush=True)\nsys.exit(7)"
        )
        self.runtime.start("crash", program, 60)
        result = self.wait_until_stopped("crash")
        self.assertFalse(result["ok"])
        self.assertTrue(result["crashed"])
        self.assertEqual(result["return_code"], 7)
        self.assertIn("boom", result["output"])

    def test_duration_is_bounded_and_auto_stops(self):
        program = self.program(
            "bounded.py", "import time\nprint('ready', flush=True)\ntime.sleep(30)"
        )
        started = self.runtime.start("bounded", program, 0.01)
        self.assertEqual(started["duration_seconds"], 1.0)
        result = self.wait_until_stopped("bounded")
        self.assertFalse(result["running"])

    def test_startup_handshake_rejects_immediate_crash(self):
        program = self.program(
            "startup_crash.py", "import sys\nprint('import failed', flush=True)\nsys.exit(9)"
        )
        result = self.runtime.start(
            "startup-crash", program, 30, interpreter=sys.executable,
            startup_grace=0.2,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["running"])
        self.assertTrue(result["crashed"])
        self.assertEqual(result["return_code"], 9)
        self.assertIn("import failed", result["output"])

    def test_stop_does_not_relabel_a_prior_crash(self):
        program = self.program(
            "late_crash.py", "import sys, time\ntime.sleep(0.05)\nsys.exit(4)"
        )
        started = self.runtime.start(
            "late-crash", program, 30, interpreter=sys.executable
        )
        self.assertTrue(started["running"])
        time.sleep(0.15)
        result = self.runtime.stop("late-crash")
        self.assertFalse(result["ok"])
        self.assertTrue(result["crashed"])
        self.assertEqual(result["reason"], "crashed")
        self.assertEqual(result["return_code"], 4)

    def test_explicit_interpreter_is_reported(self):
        program = self.program(
            "explicit_python.py", "import time\nprint('ready', flush=True)\ntime.sleep(30)"
        )
        started = self.runtime.start(
            "explicit-python", program, 30, interpreter=sys.executable,
            startup_grace=0.1,
        )
        self.assertTrue(started["ready"])
        self.assertEqual(started["interpreter"], sys.executable)
        self.assertTrue(started["run_id"].startswith("run-"))
        self.runtime.stop("explicit-python")

    def test_second_start_cannot_orphan_existing_runtime(self):
        program = self.program(
            "single_runtime.py", "import time\ntime.sleep(30)"
        )
        first = self.runtime.start(
            "single", program, 30, interpreter=sys.executable,
            startup_grace=0.1,
        )
        second = self.runtime.start(
            "single", program, 30, interpreter=sys.executable,
            startup_grace=0.1,
        )
        self.assertTrue(first["running"])
        self.assertFalse(second["ok"])
        status = self.runtime.status("single")
        self.assertEqual(status["run_id"], first["run_id"])
        self.assertTrue(status["running"])


if __name__ == "__main__":
    unittest.main()
