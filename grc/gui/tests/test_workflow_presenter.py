"""Headless presenter contracts for the Workflow status panel.

These tests exercise the text and control state derived from a workflow digest
without constructing a real GTK window.  Visual layout remains a manual check.
"""

import unittest

from grc.gui.ClaimsPanel import ClaimsPanel
from grc.gui.workflow_presenter import present


class _Widget:
    def __init__(self):
        self.text = ""
        self.label = ""
        self.visible = None
        self.sensitive = None

    def set_text(self, value):
        self.text = value

    def set_label(self, value):
        self.label = value

    def set_visible(self, value):
        self.visible = bool(value)

    def set_sensitive(self, value):
        self.sensitive = bool(value)


class _PresenterHarness:
    _set_runtime_line = ClaimsPanel._set_runtime_line
    _set_activity = ClaimsPanel._set_activity
    _set_pending = ClaimsPanel._set_pending
    refresh_runtime = ClaimsPanel.refresh_runtime
    _format_si = staticmethod(ClaimsPanel._format_si)
    _default_details = staticmethod(ClaimsPanel._default_details)

    def __init__(self):
        self._activity_label = _Widget()
        self._runtime_label = _Widget()
        self._runtime_controls = _Widget()
        self._pending_label = _Widget()
        self._pending_row = _Widget()
        self._confirm_btn = _Widget()
        self._cancel_btn = _Widget()
        self._evidence_btn = _Widget()
        self._retry_btn = _Widget()
        self._last_spec = {}
        self._last_pending = {}
        self.evidence_path = ""


class WorkflowPresenterTest(unittest.TestCase):
    def setUp(self):
        self.panel = _PresenterHarness()

    def test_activity_and_runtime_show_current_control_state(self):
        workflow = {
            "task_label": "配置 SDR",
            "task_type": "HARDWARE_CONFIGURE",
            "stage_label": "有限时长发射",
            "stage_index": 6,
            "stage_total": 8,
            "execution_status": "waiting_user",
            "wait_kind": "approval",
            "runtime": {
                "status": "running",
                "running": True,
                "pid": 321,
                "run_id": "run-test123",
                "remaining_seconds": 12.5,
                "max_duration_seconds": 30,
                "do_not_run_grc": True,
                "log_tail": "ready\nUUU",
            },
        }

        self.panel._set_activity({}, workflow)

        self.assertIn("配置 SDR", self.panel._activity_label.text)
        self.assertIn("有限时长发射 6/8", self.panel._activity_label.text)
        self.assertIn("等待批准", self.panel._activity_label.text)
        self.assertIn("run_id=run-test123", self.panel._runtime_label.text)
        self.assertIn("剩余 12.5s", self.panel._runtime_label.text)
        self.assertNotIn("pid=", self.panel._runtime_label.text)
        self.assertNotIn("UUU", self.panel._runtime_label.text)
        self.assertTrue(self.panel._runtime_label.visible)
        self.assertTrue(self.panel._runtime_controls.visible)

    def test_workflow_card_hides_internal_bookkeeping(self):
        workflow = {
            "workflow_id": "wf-test",
            "revision": 3,
            "base_project_version": 2,
            "current_stage": "offline_protocol_verify",
            "runtime": {
                "status": "stopped",
                "run_id": "run-test123",
                "pid": 321,
                "remaining_seconds": 0,
                "return_code": 0,
            },
            "stages": [
                {
                    "id": "build_ble_advertiser",
                    "label": "构建 BLE 广播",
                    "execution_status": "completed",
                    "outcome": "passed",
                    "attempt": 1,
                    "max_attempts": 1,
                    "completion": ["ble_packet_created"],
                    "completion_result": {"ble_packet_created": True},
                },
                {
                    "id": "offline_protocol_verify",
                    "label": "BLE 离线协议校验",
                    "execution_status": "running",
                    "attempt": 1,
                    "max_attempts": 1,
                    "completion": ["ble_packet_valid"],
                    "completion_result": {},
                },
            ],
        }

        card = present(spec={}, workflow=workflow, claims=[])["workflow"]
        text = str(card)

        self.assertTrue(card["visible"])
        self.assertEqual(card["stages"][0]["status"], "passed")
        self.assertEqual(card["stages"][1]["status"], "running")
        self.assertEqual(card["stages"][0]["acceptance_count"], 1)
        self.assertNotIn("workflow_id", text)
        self.assertNotIn("attempt", text)
        self.assertNotIn("completion_result", text)
        self.assertNotIn("return_code", text)
        self.assertNotIn("pid", text)

    def test_inspector_failed_outcome_not_masked_by_completion(self):
        workflow = {
            "workflow_id": "wf-hw",
            "stages": [
                {
                    "id": "tx_build_and_validate",
                    "label": "发射机构建与校验",
                    "execution_status": "completed",
                    "outcome": "failed",
                    "attempt": 1,
                    "max_attempts": 2,
                    "completion": ["a", "b", "c", "d"],
                    "completion_result": {"a": True, "b": True, "c": True, "d": True},
                },
            ],
        }
        card = present(spec={}, workflow=workflow, claims=[])["workflow"]
        self.assertEqual(card["stages"][0]["status"], "failed")
        self.assertEqual(card["stages"][0]["acceptance_count"], 4)

    def test_recovery_buttons_are_actionable(self):
        self.panel._set_pending({
            "action": "stage_recovery",
            "reason": "当前 Stage 未满足完成条件",
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "重试本阶段")
        self.assertEqual(self.panel._cancel_btn.label, "取消任务")
        self.assertTrue(self.panel._pending_row.visible)

    def test_completion_count_is_projected_as_acceptance_predicates(self):
        card = present(spec={}, claims=[], workflow={
            "current_stage": "verify",
            "stages": [{
                "id": "verify", "label": "校验", "execution_status": "running",
                "attempt": 3, "max_attempts": 5,
                "completion": ["packet_valid", "parameters_match"],
            }],
        })["workflow"]
        self.assertEqual(card["stages"][0]["acceptance_count"], 2)
        self.assertNotIn("attempt", str(card))

    def test_checkpoint_buttons_are_stage_specific(self):
        self.panel._set_pending({
            "action": "rf_plan_confirmation",
            "purpose": "rf_authorization",
            "requested_effect": "RF_RUN",
            "max_duration_seconds": 30,
            "device": {"type": "pluto", "identity": "usb:test.pluto"},
            "center_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "bandwidth": 2_000_000.0,
            "tx_attenuation": 30.0,
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "批准有限时长发射")
        self.assertEqual(self.panel._cancel_btn.label, "取消")
        self.assertIn("最长 30 秒", self.panel._pending_label.text)
        self.assertIn("usb:test.pluto", self.panel._pending_label.text)
        self.assertIn("2.402 GHz", self.panel._pending_label.text)
        self.assertIn("2 Msps", self.panel._pending_label.text)
        self.assertIn("衰减 30.0 dB", self.panel._pending_label.text)
        self.assertFalse(self.panel._evidence_btn.visible)

        self.panel._set_pending({
            "action": "rf_plan_confirmation",
            "purpose": "config_handoff",
            "device": {"type": "pluto", "identity": "usb:test.pluto"},
            "center_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "bandwidth": 2_000_000.0,
            "tx_attenuation": 30.0,
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "确认已保存")
        self.assertIn("不启动射频", self.panel._pending_label.text)
        self.assertNotIn("受控发射", self.panel._pending_label.text)

        self.panel._set_pending({
            "action": "rf_plan_confirmation",
            "purpose": "device_configuration",
            "requested_effect": "DEVICE_READ",
            "device": {"type": "pluto", "identity": "usb:test.pluto"},
            "center_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "bandwidth": 2_000_000.0,
            "tx_attenuation": 30.0,
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "确认配置")
        self.assertIn("不启动射频", self.panel._pending_label.text)
        self.assertNotIn("受控发射", self.panel._pending_label.text)

        self.panel._set_pending({
            "action": "over_air_verification",
            "purpose": "ota_observation",
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "已看到目标名称")
        self.assertEqual(self.panel._cancel_btn.label, "未看到")
        self.assertTrue(self.panel._evidence_btn.visible)
        self.assertTrue(self.panel._evidence_btn.sensitive)
        self.assertIn("人工确认、附件缺失", self.panel._pending_label.text)

    def test_quality_warning_is_visible_without_raw_warning_dump(self):
        workflow = {
            "task_label": "有限时长发射",
            "stage_label": "运行观察",
            "stage_index": 1,
            "stage_total": 1,
            "execution_status": "completed",
            "quality": "warning",
            "control_state": {
                "warnings": [{
                    "code": "rf_stream_quality",
                    "underrun_count": 3,
                    "overrun_count": 0,
                }],
            },
            "stages": [],
        }
        self.panel._set_activity({}, workflow)
        self.assertIn("质量: warning", self.panel._activity_label.text)
        runtime = present(spec={}, workflow=workflow, claims=[])["runtime"]
        self.assertEqual(runtime["quality"], "warning")
        self.assertNotIn("rf_stream_quality", str(runtime))

    def test_completed_digest_clears_stale_checkpoint_buttons(self):
        self.panel._last_pending = {
            "action": "rf_plan_confirmation",
            "checkpoint_id": "cp-old",
            "requested_effect": "DEVICE_READ",
        }
        self.panel.refresh_runtime({
            "execution_status": "completed",
            "wait_kind": "",
            "current_stage": "rf_plan_confirmation",
            "checkpoint_id": "",
            "stages": [],
        })
        self.assertFalse(self.panel._pending_row.visible)
        self.assertEqual(self.panel._confirm_btn.label, "")

    def test_paper_view_exposes_phase_spec_sources_and_unresolved_fields(self):
        view = present(
            spec={
                "intent_status": "awaiting_input",
                "radio_specification": [
                    {"key": "goal", "label": "Goal", "value": "BLE advertising", "source": "user"},
                    {"key": "carrier_frequency", "label": "Carrier", "display_value": "2.402 GHz", "source": "protocol_default"},
                    {"key": "success_conditions", "label": "Success condition", "unresolved": True},
                ],
            },
            workflow={
                "task_type": "INTENT_ALIGNMENT",
                "current_stage": "intent_alignment",
                "shared_intent": {"status": "awaiting_input"},
            },
            claims=[],
        )
        self.assertEqual(view["phase"]["label"], "ALIGN INTENT")
        rows = {item["key"]: item for item in view["specification"]["rows"]}
        self.assertEqual(rows["goal"]["source"], "User")
        self.assertEqual(rows["carrier_frequency"]["source"], "Protocol Default")
        self.assertEqual(rows["success_conditions"]["value"], "?")
        self.assertFalse(view["specification"]["aligned"])

    def test_diagnosis_card_only_uses_scoped_findings(self):
        view = present(
            spec={}, claims=[{
                "id": "old-ble", "statement": "old BLE packet passed",
                "layer": "protocol", "status": "Passed",
                "intent_id": "intent-old",
            }],
            workflow={
                "task_type": "DIAGNOSE",
                "shared_intent": {"intent_id": "intent-current"},
                "diagnosis": {
                    "requested_dimensions": ["device", "runtime"],
                    "findings": [
                        {"check_id": "device.discovery", "dimension": "device", "status": "pass", "observation": {"identity": "usb:pluto"}},
                        {"check_id": "runtime.process_status", "dimension": "runtime", "status": "unknown", "observation": {"status": "not_started"}, "remediation": "启动后再检查"},
                    ],
                }
            },
        )
        diagnosis = view["diagnosis"]
        self.assertTrue(diagnosis["visible"])
        self.assertEqual([item["status"] for item in diagnosis["findings"]], ["Passed", "Unknown"])
        self.assertNotIn("EVM", str(diagnosis))
        self.assertEqual(view["claims"]["rows"], [])


if __name__ == "__main__":
    unittest.main()
