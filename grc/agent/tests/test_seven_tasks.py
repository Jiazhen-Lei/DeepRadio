import tempfile
import unittest

from grc.agent.state import SharedState
from grc.agent.workflow import WorkflowEngine


def project_state():
    state = SharedState(session_id="seven-task")
    state.project.grc_path = "/tmp/current.grc"
    state.project.config.update({"recipe": "bpsk_awgn", "modulation": "bpsk"})
    return state


class SevenTaskTextTest(unittest.TestCase):
    def engine(self):
        return WorkflowEngine(tempfile.mktemp(suffix=".yaml"))

    def assert_task(self, text, expected, state=None):
        workflow = self.engine().consume_turn(text, state or SharedState())
        self.assertEqual(workflow.task_type, expected)
        self.assertTrue(workflow.current_stage)
        return workflow

    def test_end_to_end_sim_text(self):
        self.assert_task("构建 BPSK 过 AWGN 并测 EVM", "END_TO_END_SIM")

    def test_tx_build_text(self):
        self.assert_task("构建一个 QPSK 发射机", "TX_BUILD")

    def test_rx_build_text(self):
        self.assert_task("构建 BPSK 接收机并测 BER", "RX_BUILD")

    def test_diagnose_text(self):
        self.assert_task("诊断当前工程 EVM 偏高原因，先不要修改", "DIAGNOSE", project_state())

    def test_modify_project_text(self):
        workflow = self.assert_task("把当前 BPSK 改成 QPSK", "MODIFY_PROJECT", project_state())
        self.assertEqual(workflow.current_stage, "inspect_and_plan")

    def test_observe_text(self):
        self.assert_task("查看当前工程频谱和星座图", "OBSERVE", project_state())

    def test_hardware_configure_text(self):
        workflow = self.assert_task(
            "配置 USRP B210，中心频率 2.4 GHz，采样率 1 MHz",
            "HARDWARE_CONFIGURE",
        )
        self.assertEqual(workflow.current_stage, "hardware_precheck")


# --- seven_task_text_variants.py ---

"""Seventy Text variants covering the seven Task Types plus hardware RX observe."""


VARIANTS = [
    ("构建 BPSK 过 AWGN 并测 EVM", "END_TO_END_SIM"),
    ("做一个 QPSK AWGN 链路看星座", "END_TO_END_SIM"),
    ("生成 OFDM 过高斯噪声测 EVM", "END_TO_END_SIM"),
    ("build bpsk awgn and measure evm", "END_TO_END_SIM"),
    ("请搭一个 BPSK 加噪声的通信系统", "END_TO_END_SIM"),
    ("创建 QPSK 仿真并看眼图", "END_TO_END_SIM"),
    ("做一个 OFDM AWGN 端到端仿真", "END_TO_END_SIM"),
    ("生成 BPSK 波形过 AWGN", "END_TO_END_SIM"),
    ("构建 qpsk 通信链路测 evm", "END_TO_END_SIM"),
    ("做一个带 AWGN 的 BPSK 系统", "END_TO_END_SIM"),
    ("create ofdm awgn and plot constellation", "END_TO_END_SIM"),
    ("生成一个 BPSK 加性噪声仿真", "END_TO_END_SIM"),
    ("构建一个 QPSK 发射机", "TX_BUILD"),
    ("生成 BPSK transmitter", "TX_BUILD"),
    ("做一个 OFDM 发射链", "TX_BUILD"),
    ("build a qpsk transmitter", "TX_BUILD"),
    ("创建 BPSK 发射机流图", "TX_BUILD"),
    ("构建 QPSK TX 链路", "TX_BUILD"),
    ("生成 ofdm transmitter", "TX_BUILD"),
    ("做一个 BPSK 发射机不要接收", "TX_BUILD"),
    ("创建 qpsk 发射机", "TX_BUILD"),
    ("build bpsk tx chain", "TX_BUILD"),
    ("构建 BPSK 接收机并测 BER", "RX_BUILD"),
    ("做一个 QPSK receiver 看误码率", "RX_BUILD"),
    ("生成 OFDM 接收机", "RX_BUILD"),
    ("build a bpsk receiver and measure ber", "RX_BUILD"),
    ("创建 BPSK 解调接收机", "RX_BUILD"),
    ("构建 QPSK 接收机并测 BER", "RX_BUILD"),
    ("做一个接收机并解调 BPSK", "RX_BUILD"),
    ("生成 bpsk receiver", "RX_BUILD"),
    ("使用usrpb210构建接收机，在2.402GHz绘制出实时的频谱图", "RX_BUILD"),
    ("用 USRP B210 构建接收机在 2.4 GHz 看实时频谱", "RX_BUILD"),
    ("诊断当前工程 EVM 偏高原因，先不要修改", "DIAGNOSE"),
    ("排查当前流图为什么 BER 差", "DIAGNOSE"),
    ("当前工程故障请诊断", "DIAGNOSE"),
    ("diagnose the current flowgraph evm", "DIAGNOSE"),
    ("为什么当前工程星座散了", "DIAGNOSE"),
    ("诊断当前工程问题但先不要改", "DIAGNOSE"),
    ("诊断 EVM 异常", "DIAGNOSE"),
    ("排查当前 GRC 故障", "DIAGNOSE"),
    ("把当前 BPSK 改成 QPSK", "MODIFY_PROJECT"),
    ("把当前工程换成 OFDM", "MODIFY_PROJECT"),
    ("修改当前工程为 QPSK", "MODIFY_PROJECT"),
    ("change the current project to qpsk", "MODIFY_PROJECT"),
    ("将当前 BPSK 换成 qpsk_awgn", "MODIFY_PROJECT"),
    ("调参把当前工程改成 OFDM", "MODIFY_PROJECT"),
    ("修改现有流图为 QPSK", "MODIFY_PROJECT"),
    ("把当前配方换成 qpsk", "MODIFY_PROJECT"),
    ("查看当前工程频谱和星座图", "OBSERVE"),
    ("观察当前工程眼图", "OBSERVE"),
    ("measure current spectrum", "OBSERVE"),
    ("查看当前流图频谱", "OBSERVE"),
    ("观察当前工程 constellation", "OBSERVE"),
    ("测量当前工程频谱", "OBSERVE"),
    ("查看当前工程眼图", "OBSERVE"),
    ("measure the current constellation", "OBSERVE"),
    ("配置 USRP B210，中心频率 2.4 GHz，采样率 1 MHz", "HARDWARE_CONFIGURE"),
    ("配置 SDR 硬件使用 B210", "HARDWARE_CONFIGURE"),
    ("设置 Pluto 中心频率 915 MHz 采样率 1 Msps", "HARDWARE_CONFIGURE"),
    ("configure usrp b210 at 2.4 ghz sample rate 2 msps", "HARDWARE_CONFIGURE"),
    ("配置 HackRF 硬件 中心频率 433 MHz 采样率 2 MHz", "HARDWARE_CONFIGURE"),
    ("用 B210 发射 BLE 信号，localname 为 deepradio，让 LightBlue 收到", "HARDWARE_CONFIGURE"),
    ("部署 BLE 广播到 USRP B210 localname 为 demo", "HARDWARE_CONFIGURE"),
    ("配置 LimeSDR 中心频率 1 GHz 采样率 5 MHz", "HARDWARE_CONFIGURE"),
    ("设置 USRP 硬件载频 2.45 GHz 采样率 1e6", "HARDWARE_CONFIGURE"),
    ("配置 b210 采样率 2 Msps 中心频率 2.402 GHz", "HARDWARE_CONFIGURE"),
    ("硬件配置 Pluto 中心频率 2.4GHz 采样率 3e6", "HARDWARE_CONFIGURE"),
    ("configure sdr pluto carrier 915mhz sample rate 1msps", "HARDWARE_CONFIGURE"),
    ("给当前工程配置 USRP B210 中心频率 2.4 GHz 采样率 1 MHz", "HARDWARE_CONFIGURE"),
    ("配置硬件 HackRF 载频 915 MHz 采样率 2 Msps", "HARDWARE_CONFIGURE"),
    (
        "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认。",
        "HARDWARE_CONFIGURE",
    ),
    (
        "给 Pluto 配好发射流图，载频 915 MHz 采样率 2 Msps，先不要发射",
        "HARDWARE_CONFIGURE",
    ),
]


# --- test_seven_task_text_variants.py ---

import tempfile
import unittest

from grc.agent.state import SharedState
from grc.agent.workflow import WorkflowEngine


def _state_for(expected: str) -> SharedState:
    state = SharedState(session_id="variants")
    if expected in {"DIAGNOSE", "MODIFY_PROJECT", "OBSERVE"}:
        state.project.grc_path = "/tmp/current.grc"
        state.project.config.update({"recipe": "bpsk_awgn", "modulation": "bpsk"})
    return state


class SevenTaskTextVariantTest(unittest.TestCase):
    def test_variant_count_is_at_least_seventy(self):
        self.assertGreaterEqual(len(VARIANTS), 70)

    def test_each_variant_classifies_to_expected_task(self):
        engine = WorkflowEngine(tempfile.mktemp(suffix=".yaml"))
        self.assertEqual(len(VARIANTS), len({item[0] for item in VARIANTS}))
        for text, expected in VARIANTS:
            with self.subTest(text=text):
                intent = engine.classify(text, _state_for(expected))
                self.assertEqual(intent.task_type, expected)


# --- test_seven_task_service_agent.py ---

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grc.agent import env
from grc.agent.state import SharedState
from grc.agent.service import session_store as store
from grc.agent.service.adapter import ServiceAgent


class SevenTaskServiceAgentTest(unittest.TestCase):
    def setUp(self):
        try:
            self.platform = env.make_platform()
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"make_platform unavailable: {exc}")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True)
        self._sessions_patch = mock.patch.object(
            store, "sessions_root", return_value=str(self.sessions)
        )
        self._agent_patch = mock.patch(
            "grc.agent.service.orchestrator.build_agent", return_value=None
        )
        self._sessions_patch.start()
        self._agent_patch.start()

    def tearDown(self):
        self._agent_patch.stop()
        self._sessions_patch.stop()
        self.temp.cleanup()

    def agent(self, session_id: str) -> ServiceAgent:
        return ServiceAgent(session_id=session_id, platform=self.platform)

    def test_end_to_end_then_diagnose_observe_modify(self):
        agent = self.agent("seven-e2e")
        built = agent.step("构建 BPSK 过 AWGN 并测 EVM，要求 EVM 小于 10%")
        digest = built.workflow_digest
        self.assertEqual(digest["task_type"], "END_TO_END_SIM")
        self.assertTrue(Path(built.artifacts["grc_path"]).is_file())
        self.assertGreaterEqual(int(digest.get("base_project_version") or 0), 1)
        if digest.get("execution_status") == "completed":
            self.assertTrue(built.done)
        built_version = int(agent._state.project.flowgraph_version)

        diagnosed = agent.step(
            "诊断当前链路的 EVM，解释主要原因并给出最小修改建议，先保持工程不变。"
        )
        self.assertEqual(diagnosed.workflow_digest["task_type"], "DIAGNOSE")
        self.assertNotEqual(
            diagnosed.workflow_digest["current_stage"], "repair_confirmation"
        )
        self.assertEqual(agent._state.project.flowgraph_version, built_version)
        self.assertNotIn("eye_png", diagnosed.artifacts or {})
        evm = ((diagnosed.artifacts or {}).get("metrics") or {}).get("evm_pct")
        if evm is not None and float(evm) < 10:
            self.assertNotIn("偏高", diagnosed.text or "")
            self.assertTrue(
                "达标" in (diagnosed.text or "")
                or "设计噪声" in (diagnosed.text or "")
                or "不是故障" in (diagnosed.text or "")
            )

        observed = agent.step("查看当前工程频谱和星座图")
        self.assertEqual(observed.workflow_digest["task_type"], "OBSERVE")
        self.assertEqual(
            observed.workflow_digest["current_stage"], "inspect_and_measure"
        )
        self.assertEqual(agent._state.project.flowgraph_version, built_version)

        modified = agent.step("把当前 BPSK 改成 QPSK")
        self.assertEqual(modified.workflow_digest["task_type"], "MODIFY_PROJECT")
        self.assertIn(
            modified.workflow_digest["current_stage"],
            ("inspect_and_plan", "change_confirmation"),
        )
        self.assertFalse(
            bool((agent._tool_ctx.extra if agent._tool_ctx else {}).get(
                "mutation_forbidden"
            ))
        )
        self.assertEqual(agent._state.project.config.get("recipe"), "bpsk_awgn")
        self.assertEqual(modified.workflow_digest.get("wait_kind"), "approval")
        checkpoint_id = modified.workflow_digest.get("checkpoint_id") or ""
        self.assertTrue(checkpoint_id)
        approved = agent.step_command({
            "action": "checkpoint_decision",
            "checkpoint_id": checkpoint_id,
            "decision": "approved",
        })
        self.assertEqual(agent._state.project.config.get("recipe"), "qpsk_awgn")
        self.assertEqual(agent._state.project.config.get("modulation"), "qpsk")
        self.assertGreater(agent._state.project.flowgraph_version, built_version)
        grc_path = Path(
            approved.artifacts.get("grc_path")
            or agent._state.project.grc_path
        )
        self.assertTrue(grc_path.is_file())
        self.assertRegex(grc_path.read_text(encoding="utf-8"), r"type:\s*qpsk")
        self.assertNotEqual(
            approved.workflow_digest.get("wait_kind"), "denied"
        )

        events_path = Path(store.session_root("seven-e2e")) / "events.jsonl"
        records = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertGreaterEqual(len(records), 2)
        self.assertEqual(
            [item["seq"] for item in records], list(range(1, len(records) + 1))
        )
        self.assertTrue(any(item.get("workflow_id") for item in records))
        self.assertTrue(any(item.get("stage_id") for item in records))

    def test_denied_design_link_does_not_bump_version(self):
        from grc.agent.tools.design_link import design_link

        agent = self.agent("deny-bump")
        agent.step("构建 BPSK 过 AWGN 并测 EVM")
        version = int(agent._state.project.flowgraph_version)
        recipe = agent._state.project.config.get("recipe")
        ctx = agent._make_ctx()
        ctx.extra["mutation_forbidden"] = True
        ctx.extra["state"] = agent._state
        result = design_link(ctx, recipe="qpsk_awgn")
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("policy"), "DENY")
        self.assertEqual(agent._state.project.flowgraph_version, version)
        self.assertEqual(agent._state.project.config.get("recipe"), recipe)

    def _assert_sim_only_tx_reply(self, session_id: str, text: str) -> None:
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            reply = self.agent(session_id).step(text)
        self.assertNotIn(
            "hardware_configure",
            reply.workflow_digest.get("capabilities") or [],
        )
        self.assertNotIn(
            reply.workflow_digest.get("current_stage"),
            ("hardware_precheck", "hardware_confirmation", "configure_and_check"),
        )
        self.assertTrue(Path(reply.artifacts["grc_path"]).is_file())
        events = (
            Path(store.session_root(session_id)) / "events.jsonl"
        ).read_text(encoding="utf-8")
        self.assertNotIn("discover_devices", events)
        self.assertNotIn("start_flowgraph", events)

    def test_tx_build_produces_flowgraph(self):
        agent = self.agent("seven-tx")
        reply = agent.step("构建一个 QPSK 发射机")
        self.assertEqual(reply.workflow_digest["task_type"], "TX_BUILD")
        self.assertTrue(Path(reply.artifacts["grc_path"]).is_file())

    def test_sim_only_tx_text_does_not_open_hardware(self):
        self._assert_sim_only_tx_reply(
            "seven-tx-sim-only",
            "构建一个 QPSK 基带发射链路，只做仿真，不接真实硬件。",
        )

    def test_open_negation_tx_does_not_open_hardware(self):
        self._assert_sim_only_tx_reply(
            "seven-tx-open-negation",
            "先别接板子，只仿真，搭一个 QPSK 发射链路",
        )

    def test_rx_build_produces_flowgraph(self):
        agent = self.agent("seven-rx")
        reply = agent.step("构建 BPSK 接收机并测 BER")
        self.assertEqual(reply.workflow_digest["task_type"], "RX_BUILD")
        grc_path = Path(reply.artifacts["grc_path"])
        self.assertTrue(grc_path.is_file())
        self.assertNotRegex(
            grc_path.read_text(encoding="utf-8"),
            r"file:\s+/.+_rx\.bin",
        )
        ber = ((reply.artifacts or {}).get("metrics") or {}).get("ber")
        if ber is not None and ber == ber:
            self.assertLess(float(ber), 0.1)

    def test_hardware_configure_stays_config_only(self):
        agent = self.agent("seven-hw")
        reply = agent.step("配置 USRP B210，中心频率 2.4 GHz，采样率 1 MHz")
        self.assertEqual(reply.workflow_digest["task_type"], "HARDWARE_CONFIGURE")
        self.assertIn(
            reply.workflow_digest["current_stage"],
            ("hardware_precheck", "hardware_confirmation", "configure_and_check"),
        )
        self.assertFalse(reply.done)
        events_path = Path(store.session_root("seven-hw")) / "events.jsonl"
        text = events_path.read_text(encoding="utf-8")
        self.assertNotIn("start_flowgraph", text)

    def test_pluto_tx_flowgraph_stays_hardware_configure(self):
        agent = self.agent("seven-hw-txfg")
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            reply = agent.step(
                "为 PlutoSDR 配置 2.402 GHz、2 Msps 的发射流图，保存配置并停在发射确认。"
            )
        self.assertEqual(reply.workflow_digest["task_type"], "HARDWARE_CONFIGURE")
        self.assertNotIn("modulation", reply.workflow_digest.get("missing_slots") or [])
        self.assertNotIn(
            "无可证明满足全部硬件能力的确定性模板",
            reply.text or "",
        )
        self.assertFalse(reply.done)

    def test_input_wait_is_projected_to_persisted_shared_state(self):
        agent = self.agent("seven-input-wait")
        reply = agent.step("帮我配置 USRP B210")
        self.assertEqual(reply.workflow_digest["wait_kind"], "input")
        restored = SharedState.load(
            store.state_path("seven-input-wait"),
            session_id="seven-input-wait",
        )
        self.assertIn("carrier_frequency", restored.spec.open_questions)
        self.assertIn("sample_rate", restored.spec.open_questions)

    def test_checkpoint_command_does_not_create_a_confirmation_turn(self):
        agent = self.agent("seven-command")
        text = "配置 USRP B210，中心频率 2.4 GHz，采样率 1 MHz"
        waiting = agent.step(text)
        checkpoint_id = waiting.workflow_digest["checkpoint_id"]
        self.assertTrue(checkpoint_id)
        approved = agent.step_command({
            "action": "checkpoint_decision",
            "checkpoint_id": checkpoint_id,
            "decision": "approved",
        })
        self.assertEqual(agent._workflow.workflow.intent.raw_text, text)
        self.assertEqual(approved.workflow_digest["execution_status"], "completed")
        events_path = Path(store.session_root("seven-command")) / "events.jsonl"
        records = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        turns = [item for item in records if item["event"] == "user_turn_received"]
        self.assertEqual(len(turns), 1)


if __name__ == "__main__":
    unittest.main()
