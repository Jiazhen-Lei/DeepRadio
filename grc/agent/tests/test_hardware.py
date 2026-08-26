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
            stage = workflow.stage("over_air_verification")
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
            stage = workflow.stage("over_air_verification")
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
            self.assertIn("不在有效运行窗口", reply.text)
            self.assertEqual(stage.execution_status, "waiting")


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
from grc.agent.schema import AgentReply


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
        engine._activate_current()
        engine.resolve_checkpoint("approved")
        stage = workflow.stage("rf_plan_confirmation")
        self.assertEqual(stage.outcome, "passed")
        self.assertTrue(stage.result["completion"]["rf_plan_approved"])
        digest = engine.digest()
        rf = next(item for item in digest["stages"] if item["id"] == "rf_plan_confirmation")
        self.assertTrue(rf["completion_result"]["rf_plan_approved"])

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
        self.assertTrue(checks["hardware_check_completed"])

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
                "用plutosdr发射一段2.402GHz的ble信号，local name为loveu"
            )
        events = (self.sessions / "ble-events" / "events.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn('"event": "stage_routed"', events)
        self.assertIn('"event": "deterministic_handler_started"', events)
        self.assertNotIn('"event": "subagent_invoked"', events)
        self.assertIn("BLE 1M", reply.spec_digest.get("summary") or "")
        self.assertIn("loveu", reply.spec_digest.get("summary") or "")
        self.assertIn("✓ BLE PDU generated", reply.text or "")


# --- test_tool_origin_and_profiles.py ---

"""Contracts for tool origin labels and HardwareProfile device args."""


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
        self.assertEqual(workflow.stages[-1].id, "stop_runtime")

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


# --- test_hardware_runtime_contracts.py ---

import sys
import tempfile
import time
import unittest
from pathlib import Path

from grc.agent.service.hardware_runtime import HardwareRuntime


class HardwareRuntimeContractTest(unittest.TestCase):
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
