"""P1/P2 contracts: Plan Compiler, DiagnosisExperiment, GraphPatch, MeasurementRun."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grc.agent.memory.profile import UserProfile
from grc.agent.state import Claim, ClaimStore, SharedState, attach_measurement
from grc.agent.tools.diagnosis_experiment import run_diagnosis_experiment
from grc.agent.tools.registry import ToolContext
from grc.agent.tools.state_tools import (
    check_patch_preconditions,
    expand_patch_operations,
)
from grc.agent.workflow.plan_compiler import (
    compile_stages,
    replan_tail,
    tail_needs_replan_proposal,
    validate_proposal,
)
from grc.agent.workflow.planning import (
    is_rf_grant_effect,
    stage_display_label,
)
from grc.agent.workflow.schema import Stage
from grc.agent.workflow import WorkflowEngine
from grc.agent.tests.test_seven_tasks import VARIANTS


class PlanCompilerContractTest(unittest.TestCase):
    def test_unknown_plan_node_is_rejected(self):
        accepted, rejected = validate_proposal(
            [{"id": "invented_rf_sniffer"}, {"id": "hardware_precheck"}],
            catalog={"task_candidates": {
                "HARDWARE_CONFIGURE": {
                    "stages": [{"id": "hardware_precheck"}],
                }
            }},
        )
        self.assertIn("invented_rf_sniffer", rejected)
        self.assertEqual([node.stage_id for node in accepted], ["hardware_precheck"])

    def test_replan_without_proposal_keeps_deferred(self):
        deferred = [{"id": "configure_device"}, {"id": "run_bounded"}]
        self.assertEqual(replan_tail(deferred, proposal=None), deferred)
        accepted, rejected = validate_proposal(
            [{"id": "not_a_real_action"}],
            catalog={"task_candidates": {"X": {"stages": [{"id": "configure_device"}]}}},
        )
        self.assertTrue(rejected)
        self.assertEqual(
            replan_tail(deferred, proposal=[{"id": "not_a_real_action"}]),
            deferred,
        )

    def test_replan_keeps_rf_tail_when_proposal_drops_arm(self):
        deferred = [
            {"id": "configure_device", "effect_level": "DEVICE_CONFIG"},
            {"id": "transmit_bounded", "effect_level": "RF_RUN"},
            {"id": "over_air_verification", "interaction": "checkpoint"},
            {"id": "stop_and_finalize", "effect_level": "RF_RUN", "safety_finalizer": True},
        ]
        catalog = {
            "task_candidates": {
                "HARDWARE_CONFIGURE": {
                    "stages": [{"id": "hardware_precheck"}],
                    "deploy_stages": list(deferred) + [
                        {"id": "discover_and_probe_hardware"},
                        {"id": "build_ble_advertiser"},
                        {"id": "tx_build_and_validate"},
                        {"id": "rf_plan_confirmation"},
                    ],
                }
            }
        }
        kept = replan_tail(
            deferred,
            proposal=[
                {"id": "discover_and_probe_hardware"},
                {"id": "build_ble_advertiser"},
                {"id": "hardware_precheck"},
                {"id": "tx_build_and_validate"},
                {"id": "rf_plan_confirmation"},
            ],
            catalog=catalog,
        )
        self.assertEqual([item["id"] for item in kept], [item["id"] for item in deferred])
        self.assertTrue(tail_needs_replan_proposal(deferred))
        self.assertFalse(tail_needs_replan_proposal([]))
        self.assertFalse(tail_needs_replan_proposal([
            {"id": "stop_and_finalize", "safety_finalizer": True},
        ]))

    def test_compile_attaches_plan_metadata(self):
        stages = [
            Stage.from_dict({
                "id": "inspect_and_measure",
                "completion": ["metrics_recorded"],
            })
        ]
        compiled, nodes, rejected = compile_stages(object(), stages)
        self.assertFalse(rejected)
        self.assertEqual(stages[0].objective, "inspect_and_measure")
        self.assertEqual(nodes[0].produces, ["metrics_recorded"])

    def test_effect_is_floored_by_bound_tool_metadata(self):
        stage = Stage.from_dict({
            "id": "apply_and_verify",
            "effect_level": "READ",
        })
        compile_stages(object(), [stage])
        self.assertEqual(stage.effect_level, "ARTIFACT_WRITE")

    def test_natural_language_predicate_is_unbound_not_executable(self):
        stage = Stage.from_dict({
            "id": "inspect_and_measure",
            "completion": ["measurement_completed"],
        })
        catalog = {"task_candidates": {"OBSERVE": {"stages": [
            {"id": "inspect_and_measure"},
        ]}}}
        compile_stages(
            object(),
            [stage],
            catalog=catalog,
            proposal=[{
                "id": "inspect_and_measure",
                "success_predicates": [
                    "measurement_completed", "the constellation looks good",
                ],
            }],
        )
        self.assertIn("measurement_completed", stage.success_predicates)
        self.assertEqual(
            stage.unbound_predicates, ["the constellation looks good"]
        )

    def test_config_confirm_label_is_not_rf_grant(self):
        self.assertFalse(is_rf_grant_effect("DEVICE_READ"))
        self.assertTrue(is_rf_grant_effect("RF_RUN"))
        self.assertEqual(
            stage_display_label("rf_plan_confirmation", "RF 计划确认", "DEVICE_READ"),
            "配置确认",
        )
        self.assertEqual(
            stage_display_label("rf_plan_confirmation", "RF 计划确认", "RF_RUN"),
            "RF 计划确认",
        )

    def test_prepare_does_not_get_safety_duration(self):
        intent = type("Intent", (), {
            "slots": {"operation": "prepare"},
            "capabilities": ["hardware_runtime"],
        })()
        compile_stages(intent, [])
        self.assertNotIn("duration_seconds", intent.slots)
        intent.slots["operation"] = "deploy"
        compile_stages(intent, [])
        self.assertEqual(intent.slots.get("duration_seconds"), 30.0)


class WorkflowPlannerCompatTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def engine(self):
        return WorkflowEngine(str(self.root / "workflow.yaml"))

    def test_replan_without_llm_keeps_deferred_actions(self):
        engine = self.engine()
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            workflow = engine.consume_turn("构建 BPSK AWGN 并测 EVM", SharedState())
        workflow.deferred_plan = [
            {"id": "configure_device", "interaction": "host_controlled"},
            {"id": "run_bounded", "interaction": "host_controlled"},
        ]
        engine._replan_and_materialize()
        ids = [stage.id for stage in workflow.stages] + [
            str(item.get("id") or "") for item in workflow.deferred_plan
        ]
        self.assertIn("configure_device", ids)
        self.assertIn("run_bounded", ids)

    def test_open_compound_is_outside_seven_task_variants(self):
        text = (
            "保持现有调制，只记录 EVM 和频谱，确认之前不要改噪声，"
            "也不要配置任何 SDR 或启动射频"
        )
        self.assertNotIn(text, {item[0] for item in VARIANTS})
        with mock.patch("grc.agent.llm.is_configured", return_value=False):
            workflow = self.engine().consume_turn(text, SharedState(
                session_id="open-compound"
            ))
        ids = [stage.id for stage in workflow.stages]
        self.assertNotIn("configure_device", ids)
        self.assertNotIn("run_bounded", ids)
        self.assertNotIn("hardware_runtime", workflow.intent.capabilities)
        self.assertTrue(workflow.compiled_plan)


class DiagnosisAndPatchTest(unittest.TestCase):
    def test_diagnosis_without_factors_is_ok_and_empty(self):
        with tempfile.TemporaryDirectory() as out_dir:
            ctx = ToolContext(out_dir=out_dir)
            result = run_diagnosis_experiment(ctx)
            self.assertTrue(Path(result["report_path"]).is_file())
        self.assertTrue(result["ok"])
        self.assertEqual(result["ranked"], [])
        self.assertTrue(result["restored"])
        self.assertTrue(result["project_unchanged"])

    def test_recipe_change_compiles_to_graph_patch(self):
        from grc.agent.service.stage_handlers import _recipe_graph_patch

        patch = _recipe_graph_patch("bpsk_awgn", "qpsk_awgn")
        self.assertTrue(patch["operations"])
        self.assertIn("chan", patch["preserved_block_ids"])
        self.assertTrue(any(
            item.get("op") == "set" and item.get("id") == "mod"
            for item in patch["operations"]
        ))
        self.assertFalse(any(
            item.get("op") == "remove" and item.get("id") == "chan"
            for item in patch["operations"]
        ))

    def test_recipe_patch_preserves_explicit_connection_ports(self):
        from types import SimpleNamespace
        from grc.agent.service.stage_handlers import _recipe_graph_patch

        before = SimpleNamespace(
            name="before",
            blocks=[],
            connections=[("source", "sink", 1, 2)],
        )
        after = SimpleNamespace(
            name="after",
            blocks=[],
            connections=[("source", "sink", 3, 4)],
        )
        with mock.patch(
            "grc.agent.knowledge.recipes.get_recipe",
            side_effect=[before, after],
        ):
            patch = _recipe_graph_patch("before", "after")

        self.assertIn({
            "op": "disconnect", "src_id": "source", "src_port": 1,
            "dst_id": "sink", "dst_port": 2,
        }, patch["operations"])
        self.assertIn({
            "op": "connect", "src_id": "source", "src_port": 3,
            "dst_id": "sink", "dst_port": 4,
        }, patch["operations"])

    def test_graph_patch_aliases_and_preconditions(self):
        expanded, error = expand_patch_operations([
            {"op": "set_param", "block": "chan", "key": "noise_voltage", "value": "0.02"},
            {
                "op": "replace_block",
                "old": "mod",
                "new": "mod",
                "key": "digital_constellation_modulator",
                "params": {"type": "qpsk"},
            },
            {"op": "connect", "src": "src", "dst": "dst"},
        ])
        self.assertFalse(error)
        self.assertEqual(expanded[0]["op"], "set")
        self.assertEqual(expanded[0]["id"], "chan")
        self.assertEqual(expanded[0]["name"], "noise_voltage")
        self.assertEqual(expanded[1]["op"], "remove")
        self.assertEqual(expanded[2]["op"], "add")
        self.assertEqual(expanded[2]["key"], "digital_constellation_modulator")
        self.assertEqual(expanded[3]["op"], "connect")
        self.assertEqual(expanded[3]["src_id"], "src")

        ctx = ToolContext()
        ctx.blocks = {}
        self.assertIn("缺少块", check_patch_preconditions(ctx, [{"block": "chan"}]))


class MeasurementAndProfileTest(unittest.TestCase):
    def test_measurement_run_shares_one_id(self):
        state = SharedState(session_id="meas")
        ctx = ToolContext()
        ctx.extra["state"] = state
        first = attach_measurement(ctx, metric="evm", result={"value": 8.0})
        second = attach_measurement(
            ctx, metric="constellation", artifact="/tmp/constellation.png"
        )
        self.assertEqual(first, second)
        self.assertEqual(len(state.measurements), 1)
        record = state.measurements[0]
        self.assertEqual(record.measurement_id, first)
        self.assertIn("/tmp/constellation.png", record.artifact_ids)
        self.assertEqual(record.result["value"], 8.0)

    def test_zero_ber_is_reported_with_finite_sample_upper_bound(self):
        from grc.agent.runtime.simulate import ber_report

        bits = [0, 1] * 256
        report = ber_report(bits, bits, max_delay=0)
        self.assertEqual(report["value"], 0.0)
        self.assertEqual(report["compared_bits"], len(bits))
        self.assertGreater(report["confidence_upper_bound"], 0.0)
        self.assertEqual(report["confidence_method"], "wilson_one_sided")

    def test_stale_claim_records_reason(self):
        state = SharedState(session_id="stale")
        state.project.flowgraph_version = 1
        store = ClaimStore(state)
        store.upsert(Claim(
            id="ber_measured",
            statement="BER measured",
            layer="sim",
            project_version=1,
        ))
        invalidated = store.invalidate_by_version(2)
        self.assertEqual(invalidated, ["ber_measured"])
        self.assertEqual(state.claims[0].status, "Stale")
        self.assertIn("project_version", state.claims[0].stale_reason)

    def test_pin_does_not_change_score_or_intent(self):
        profile = UserProfile(score=0.0)
        profile.pin("student")
        intent_slots = {"modulation": "bpsk"}
        profile.observe("EVM BER SNR PAPR matched filter Costas loop")
        self.assertEqual(profile.level, "student")
        self.assertEqual(profile.score, 0.0)
        self.assertEqual(intent_slots["modulation"], "bpsk")

    def test_ble_single_channel_capability_is_declared(self):
        from grc.agent.tools import registry

        registry.load_all()
        root = tempfile.TemporaryDirectory()
        ctx = ToolContext(out_dir=root.name)
        try:
            packet = registry.call(
                "build_ble_advertising_pdu",
                {"local_name": "deepradio", "channel": 37},
                ctx,
            )
        finally:
            root.cleanup()
        self.assertEqual(packet["capability"], "ble_advertising_single_channel")
        self.assertIn(
            "ble_advertising_three_channel",
            packet["unsupported_capabilities"],
        )
        self.assertIn("ble_independent_sniffer", packet["unsupported_capabilities"])


if __name__ == "__main__":
    unittest.main()
