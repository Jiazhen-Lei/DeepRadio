"""Headless presenter contracts for the Workflow status panel.

These tests exercise the text and control state derived from a workflow digest
without constructing a real GTK window.  Visual layout remains a manual check.
"""

import unittest

from grc.gui.ClaimsPanel import ClaimsPanel


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


class _Buffer:
    def __init__(self):
        self.text = ""

    def set_text(self, value):
        self.text = value


class _TextView:
    def __init__(self):
        self.buffer = _Buffer()

    def get_buffer(self):
        return self.buffer


class _Store(list):
    def clear(self):
        del self[:]

    def append(self, row):
        super().append(row)


class _PresenterHarness:
    _set_runtime_line = ClaimsPanel._set_runtime_line
    _set_activity = ClaimsPanel._set_activity
    _set_workflow_details = ClaimsPanel._set_workflow_details
    _set_timeline = ClaimsPanel._set_timeline
    _set_pending = ClaimsPanel._set_pending
    refresh_runtime = ClaimsPanel.refresh_runtime
    _format_si = staticmethod(ClaimsPanel._format_si)
    _default_details = staticmethod(ClaimsPanel._default_details)

    def __init__(self):
        self._activity_label = _Widget()
        self._runtime_label = _Widget()
        self._workflow_details = _TextView()
        self._timeline_store = _Store()
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
        self.assertIn("无需点击 GRC Run", self.panel._runtime_label.text)
        self.assertIn("log: UUU", self.panel._runtime_label.text)
        self.assertTrue(self.panel._runtime_label.visible)

    def test_inspector_reports_completion_and_runtime(self):
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

        self.panel._set_workflow_details(workflow)
        text = self.panel._workflow_details.buffer.text

        self.assertIn("workflow_id=wf-test", text)
        self.assertIn("runtime=stopped", text)
        self.assertIn("return_code=0", text)
        self.assertIn("构建 BLE 广播  passed", text)
        self.assertIn("attempt 1", text)
        self.assertIn("completion 1/1", text)
        self.assertIn("BLE 离线协议校验  running", text)
        self.assertIn("completion 0/1", text)

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
        self.panel._set_workflow_details(workflow)
        text = self.panel._workflow_details.buffer.text
        self.assertIn("failed", text)
        self.assertNotIn("发射机构建与校验  passed", text)

    def test_recovery_buttons_are_actionable(self):
        self.panel._set_pending({
            "action": "stage_recovery",
            "reason": "当前 Stage 未满足完成条件",
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "重试本阶段")
        self.assertEqual(self.panel._cancel_btn.label, "取消任务")
        self.assertTrue(self.panel._pending_row.visible)

    def test_timeline_keeps_execution_actor(self):
        self.panel._set_timeline([
            {
                "seq": 7,
                "event": "executor_completed",
                "stage_id": "offline_protocol_verify",
                "actor": "deterministic_stage_handler [deepradio]",
            }
        ])

        self.assertEqual(
            self.panel._timeline_store,
            [[
                "7",
                "executor_completed",
                "offline_protocol_verify",
                "deterministic_stage_handler [deepradio]",
            ]],
        )

    def test_checkpoint_buttons_are_stage_specific(self):
        self.panel._set_pending({
            "action": "rf_plan_confirmation",
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
            "action": "rf_plan_confirmation",
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
            "approved": False,
        })
        self.assertEqual(self.panel._confirm_btn.label, "已看到目标名称")
        self.assertEqual(self.panel._cancel_btn.label, "未看到")
        self.assertTrue(self.panel._evidence_btn.visible)
        self.assertTrue(self.panel._evidence_btn.sensitive)
        self.assertIn("人工确认、附件缺失", self.panel._pending_label.text)

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


if __name__ == "__main__":
    unittest.main()
