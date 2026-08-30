import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grc.agent import env
from grc.agent.state import SharedState
from grc.agent.tools import registry
from grc.agent.tools.registry import ToolContext
from grc.agent.workflow import WorkflowEngine
from grc.agent.service.adapter import ServiceAgent


class BleDeployContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = ToolContext(platform=env.make_platform(), out_dir=str(self.root))
        self.ctx.extra["state"] = SharedState(session_id="ble-test")
        registry.load_all()

    def tearDown(self):
        self.temp.cleanup()

    def test_ble_intent_builds_deploy_stage_sequence(self):
        engine = WorkflowEngine(str(self.root / "workflow.yaml"))
        workflow = engine.consume_turn(
            "用 B210 发射 BLE 信号，localname 为 deepradio，让 LightBlue 收到",
            SharedState(),
        )
        self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
        self.assertEqual(workflow.intent.slots["protocol"], "ble")
        self.assertEqual(workflow.intent.slots["local_name"], "deepradio")
        self.assertEqual(workflow.stages[0].id, "build_ble_advertiser")
        self.assertEqual(workflow.stages[-1].id, "rf_plan_confirmation")
        self.assertIn(
            "stop_and_finalize",
            [item.get("id") for item in workflow.deferred_plan],
        )

    def test_ble_packet_and_waveform_are_offline_artifacts(self):
        packet = registry.call(
            "build_ble_advertising_pdu", {"local_name": "deepradio", "channel": 37}, self.ctx
        )
        waveform = registry.call(
            "generate_ble_1m_waveform", {"local_name": "deepradio", "channel": 37}, self.ctx
        )
        verified = registry.call(
            "verify_ble_packet_bits", {"local_name": "deepradio", "channel": 37}, self.ctx
        )
        self.assertTrue(packet["ok"])
        self.assertIn("64656570726164696f", packet["pdu_hex"])
        self.assertGreater(Path(waveform["path"]).stat().st_size, 0)
        self.assertTrue(verified["valid"])

    def test_ble_packet_without_optional_local_name_is_valid(self):
        packet = registry.call(
            "build_ble_advertising_pdu", {"channel": 38}, self.ctx
        )
        waveform = registry.call(
            "generate_ble_1m_waveform", {"channel": 38}, self.ctx
        )
        verified = registry.call(
            "verify_ble_packet_bits", {"channel": 38}, self.ctx
        )
        self.assertTrue(packet["ok"])
        self.assertEqual(packet["local_name"], "")
        self.assertTrue(waveform["ok"])
        self.assertTrue(verified["valid"], verified)
        self.assertNotIn("local_name_matches_request", verified["checks"])

    def test_ble_uhd_flowgraph_is_valid_but_not_started(self):
        waveform = registry.call(
            "generate_ble_1m_waveform", {"local_name": "deepradio"}, self.ctx
        )
        built = registry.call(
            "build_ble_uhd_tx_flowgraph",
            {"waveform_path": waveform["path"], "gain": 0.0},
            self.ctx,
        )
        self.assertTrue(built["valid"])
        self.assertTrue(Path(built["grc_path"]).is_file())
        self.assertTrue(built["not_started"])

    def test_pluto_ble_intent_uses_pluto_and_channel_37(self):
        engine = WorkflowEngine(str(self.root / "workflow.yaml"))
        workflow = engine.consume_turn(
            "用plutosdr发射一段2.402GHz的ble信号，local name为deepradio，"
            "目标实现是人工可以用手机软件接收到",
            SharedState(),
        )
        self.assertEqual(workflow.task_type, "HARDWARE_CONFIGURE")
        self.assertEqual(workflow.intent.slots["hardware"], "pluto")
        self.assertEqual(workflow.intent.slots["protocol"], "ble")
        self.assertEqual(workflow.intent.slots["operation"], "deploy")
        self.assertEqual(workflow.intent.slots["local_name"], "deepradio")
        self.assertEqual(workflow.intent.slots["advertising_channels"], [37])
        self.assertEqual(workflow.intent.slots["carrier_frequency"], 2_402_000_000.0)

    def test_ble_pluto_flowgraph_is_valid_but_not_started(self):
        if "iio_pluto_sink" not in self.ctx.platform.blocks:
            self.skipTest("gr-iio Pluto sink not installed")
        waveform = registry.call(
            "generate_ble_1m_waveform", {"local_name": "deepradio", "channel": 37}, self.ctx
        )
        built = registry.call(
            "build_ble_pluto_tx_flowgraph",
            {"waveform_path": waveform["path"], "channel": 37, "attenuation": 30.0},
            self.ctx,
        )
        self.assertTrue(built.get("ok"), built.get("error") or built.get("errors"))
        self.assertTrue(built["valid"])
        self.assertTrue(Path(built["grc_path"]).is_file())
        self.assertTrue(built["not_started"])
        text = Path(built["grc_path"]).read_text(encoding="utf-8")
        self.assertIn("iio_pluto_sink", text)
        self.assertIn("2402000000", text)
        self.assertNotIn("uhd_usrp_sink", text)

    def test_service_agent_builds_pluto_flowgraph_before_hardware(self):
        if "iio_pluto_sink" not in env.make_platform().blocks:
            self.skipTest("gr-iio Pluto sink not installed")
        sessions = self.root / "sessions"
        with mock.patch(
            "grc.agent.service.session_store.sessions_root", return_value=str(sessions)
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id="ble-pluto-service")
            built = agent.step(
                "用plutosdr发射一段2.402GHz的ble信号，local name为deepradio，"
                "发射30秒，成功条件为LightBlue观察到deepradio"
            )
            self.assertIn(
                built.workflow_digest["current_stage"],
                ("discover_and_probe_device", "rf_plan_confirmation"),
            )
            self.assertTrue(Path(built.artifacts["grc_path"]).is_file())
            text = Path(built.artifacts["grc_path"]).read_text(encoding="utf-8")
            self.assertIn("iio_pluto_sink", text)
            self.assertEqual(agent._workflow.workflow.intent.raw_text,
                             "用plutosdr发射一段2.402GHz的ble信号，local name为deepradio，"
                             "发射30秒，成功条件为LightBlue观察到deepradio")
            self.assertGreaterEqual(agent._state.project.flowgraph_version, 1)
            self.assertEqual(agent._state.project.config["hardware"], "pluto")
            self.assertNotIn("modulation", agent._state.spec.open_questions)
            self.assertNotEqual(
                built.workflow_digest.get("execution_status"), "completed"
            )

    def test_rf_start_is_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GRC_AGENT_ENABLE_RF", None)
            result = registry.call(
                "start_flowgraph", {"grc_path": str(self.root / "unused.grc")}, self.ctx
            )
        self.assertFalse(result["ok"])
        self.assertFalse(result["enabled"])

    def test_gnuradio_310_runtime_uses_output_flag(self):
        grc_path = self.root / "bounded.grc"
        grc_path.write_text("options: {}\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GRC_AGENT_ENABLE_RF": "1"}), \
                mock.patch(
                    "grc.agent.tools.hardware_tools._rf_approved",
                    return_value=True,
                ), mock.patch(
                    "grc.agent.tools.hardware_tools._completion_satisfied",
                    return_value=True,
                ), mock.patch(
                    "grc.agent.tools.hardware_tools._run",
                    return_value={"ok": False, "output": "compile stopped"},
                ) as run:
            registry.call(
                "start_flowgraph", {"grc_path": str(grc_path)}, self.ctx
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["grcc", "-o"])

    def test_unsupported_ble_hardware_does_not_fallback_to_b210(self):
        sessions = self.root / "sessions"
        with mock.patch(
            "grc.agent.service.session_store.sessions_root", return_value=str(sessions)
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id="ble-hackrf-service")
            reply = agent.step(
                "用 HackRF 发射 BLE 信号，local name 为 deepradio，发射30秒，"
                "成功条件为独立接收端观察到deepradio，直接部署"
            )
            self.assertEqual(reply.workflow_digest["current_stage"],
                             "build_ble_advertiser")
            self.assertIn("暂无 BLE TX builder", reply.text)
            self.assertFalse(reply.needs_confirmation)
            self.assertFalse(any(
                item.name == "build_ble_uhd_tx_flowgraph"
                for item in reply.tool_invocations
            ))

    def test_workflow_digest_contains_full_stage_inspector_data(self):
        engine = WorkflowEngine(str(self.root / "workflow.yaml"))
        engine.consume_turn("构建 BPSK AWGN", SharedState())
        digest = engine.digest()
        self.assertEqual(len(digest["stages"]), 1)
        self.assertIn("attempt", digest["stages"][0])
        self.assertIn("completion", digest["stages"][0])

    def test_service_agent_builds_and_offline_verifies_before_hardware(self):
        sessions = self.root / "sessions"
        with mock.patch(
            "grc.agent.service.session_store.sessions_root", return_value=str(sessions)
        ), mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        ):
            agent = ServiceAgent(session_id="ble-service")
            built = agent.step(
                "用 B210 发射 BLE 信号，localname 为 deepradio，发射30秒，"
                "成功条件为LightBlue收到deepradio"
            )
            self.assertEqual(
                built.workflow_digest["current_stage"], "discover_and_probe_device"
            )
            self.assertTrue(Path(built.artifacts["grc_path"]).is_file())
            self.assertFalse(built.done)
            self.assertEqual(built.workflow_digest["wait_kind"], "recovery")
            self.assertFalse(built.needs_confirmation)


# --- test_ble_protocol_safety.py ---

"""General BLE protocol and RF safety regression tests.

The tests deliberately vary names, payload bytes, channels, sample rates, and
durations.  Production behavior is never selected by a fixed PDU or CRC.
"""


import random
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from grc.agent import env
from grc.agent.state import SharedState
from grc.agent.tools import registry
from grc.agent.tools.ble_tools import _crc24, _reference_crc24
from grc.agent.tools.hardware_profiles import resolve_hardware_profile
from grc.agent.tools.registry import ToolContext


class BleProtocolSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = ToolContext(platform=env.make_platform(), out_dir=str(self.root))
        self.ctx.extra["state"] = SharedState(session_id="ble-property-test")
        registry.load_all()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_crc_implementations_agree_for_variable_pdus(self) -> None:
        rng = random.Random(8128)
        for length in range(2, 40):
            pdu = bytes(rng.randrange(256) for _ in range(length))
            self.assertEqual(_crc24(pdu), _reference_crc24(pdu))

    def test_packet_and_iq_round_trip_for_variable_inputs(self) -> None:
        rng = random.Random(4096)
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789-_"
        for index in range(18):
            name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
            channel = (37, 38, 39)[index % 3]
            sample_rate = (2_000_000.0, 4_000_000.0)[index % 2]
            packet = registry.call(
                "build_ble_advertising_pdu",
                {"local_name": name, "channel": channel},
                self.ctx,
            )
            waveform = registry.call(
                "generate_ble_1m_waveform",
                {
                    "local_name": name,
                    "channel": channel,
                    "sample_rate": sample_rate,
                },
                self.ctx,
            )
            verified = registry.call(
                "verify_ble_packet_bits",
                {"local_name": name, "channel": channel},
                self.ctx,
            )
            self.assertTrue(packet["ok"])
            self.assertTrue(waveform["ok"])
            self.assertTrue(verified["valid"], verified)
            self.assertTrue(verified["waveform_checked"])

    def test_corruption_is_rejected(self) -> None:
        name = "variable-device"
        registry.call(
            "build_ble_advertising_pdu",
            {"local_name": name, "channel": 39},
            self.ctx,
        )
        damaged = dict(self.ctx.extra["ble_packet"])
        air = bytearray(damaged["air_packet"])
        air[len(air) // 2] ^= 0x08
        damaged["air_packet"] = bytes(air)
        self.ctx.extra["ble_packet"] = damaged
        result = registry.call(
            "verify_ble_packet_bits",
            {"local_name": name, "channel": 39},
            self.ctx,
        )
        self.assertFalse(result["valid"])
        self.assertTrue(result["failure_codes"])

    def test_tx_flowgraph_is_bounded_noninteractive_and_unarmed(self) -> None:
        waveform = registry.call(
            "generate_ble_1m_waveform",
            {"local_name": "duration-check", "channel": 37},
            self.ctx,
        )
        built = registry.call(
            "build_ble_uhd_tx_flowgraph",
            {
                "waveform_path": waveform["path"],
                "channel": 37,
                "sample_rate": 2_000_000.0,
                "duration_seconds": 7.0,
            },
            self.ctx,
        )
        self.assertTrue(built["valid"], built)
        self.assertFalse(built["armed"])
        text = Path(built["grc_path"]).read_text(encoding="utf-8")
        self.assertIn("run_options: run", text)
        self.assertIn("num_items: '14000000'", text)
        self.assertIn("state: disabled", text)

    def test_pluto_discovery_uses_usb_scan_backend(self) -> None:
        profile = resolve_hardware_profile("plutosdr")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.command(probe=False), ["iio_info", "-S", "usb"])

    def test_arming_requires_generic_workflow_gates(self) -> None:
        waveform = registry.call(
            "generate_ble_1m_waveform",
            {"local_name": "arm-check", "channel": 38},
            self.ctx,
        )
        built = registry.call(
            "build_ble_uhd_tx_flowgraph",
            {"waveform_path": waveform["path"], "channel": 38},
            self.ctx,
        )
        self.ctx.extra["state"].project.grc_path = built["grc_path"]
        denied = registry.call(
            "arm_hardware_flowgraph", {"grc_path": built["grc_path"]}, self.ctx
        )
        self.assertFalse(denied["armed"])
        self.ctx.extra["workflow"] = {
            "intent": {"slots": {"protocol": "ble"}},
            "stages": [
                {
                    "id": "offline_protocol_verify",
                    "execution_status": "completed",
                    "outcome": "passed",
                    "result": {"completion": {"ble_packet_valid": True}},
                },
                {
                    "id": "discover_and_probe_device",
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
        with mock.patch.dict("os.environ", {"GRC_AGENT_ENABLE_RF": "1"}):
            armed = registry.call(
                "arm_hardware_flowgraph",
                {"grc_path": built["grc_path"], "device_identity": "variable-serial"},
                self.ctx,
            )
        self.assertTrue(armed["armed"], armed)
        text = Path(armed["grc_path"]).read_text(encoding="utf-8")
        self.assertIn("state: enabled", text)
        self.assertIn("serial=variable-serial", text)


if __name__ == "__main__":
    unittest.main()
