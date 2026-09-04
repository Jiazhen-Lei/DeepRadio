"""Headless presenter contracts for the Workflow status panel.

These tests exercise the text and control state derived from a workflow digest
without constructing a real GTK window.  Visual layout remains a manual check.
"""

import unittest
from unittest.mock import Mock, patch

from grc.gui.AgentPanel import AgentPanel
from grc.gui.ClaimsPanel import ClaimsPanel
from grc.gui.deepradio_i18n import tr
from grc.gui.workflow_presenter import interaction_view, present


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
    _t = ClaimsPanel._t
    _set_runtime_line = ClaimsPanel._set_runtime_line
    _set_activity = ClaimsPanel._set_activity
    _set_pending = ClaimsPanel._set_pending
    refresh_runtime = ClaimsPanel.refresh_runtime
    _format_si = staticmethod(ClaimsPanel._format_si)
    _default_details = staticmethod(ClaimsPanel._default_details)

    def __init__(self):
        self._language = "en"
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

    def test_cn_translates_panel_text_without_changing_control_ids(self):
        pending = interaction_view({}, {
            "action": "stage_recovery",
            "approved": False,
        })
        self.panel._language = "cn"
        self.panel._set_pending(pending)

        self.assertEqual(self.panel._confirm_btn.label, "重试此步骤")
        self.assertEqual(self.panel._cancel_btn.label, "取消工作流")
        self.assertIn("当前步骤未通过", self.panel._pending_label.text)
        self.assertEqual(pending["action"], "stage_recovery")
        self.assertEqual(tr("en", "Workflow"), "Workflow")
        self.assertEqual(tr("cn", "Workflow"), "工作流")

    def test_new_session_stops_running_rf_before_reset(self):
        class _Runtime:
            def __init__(self):
                self.running = True
                self.commands = []

            def workflow_digest(self):
                return {"runtime": {"running": self.running}}

            def step_command(self, command):
                self.commands.append(command)
                self.running = False

        runtime = _Runtime()
        panel = type("Panel", (), {
            "_runtime": runtime,
            "_finish_new_session": lambda *_args: False,
            "_on_error": lambda *_args: False,
        })()

        with patch("grc.gui.AgentPanel.GLib.idle_add") as idle_add:
            AgentPanel._prepare_new_session(panel)

        self.assertEqual(runtime.commands, [{"action": "emergency_stop"}])
        idle_add.assert_called_once_with(panel._finish_new_session, runtime)

    def test_new_session_replaces_runtime_and_clears_the_panel(self):
        panel = Mock()
        old_runtime = Mock()
        child = Mock()
        panel._log_box.get_children.return_value = [child]
        panel._ensure_runtime.side_effect = lambda: setattr(
            panel, "_runtime", "new-runtime"
        )

        result = AgentPanel._finish_new_session(panel, old_runtime)

        self.assertFalse(result)
        self.assertEqual(panel._runtime, "new-runtime")
        self.assertEqual(panel._canvas_path, "")
        panel._log_box.remove.assert_called_once_with(child)
        panel.claims_panel.clear.assert_called_once_with()
        panel.emit.assert_called_once_with("new_session")

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
        self.assertIn("Waiting for approval", self.panel._activity_label.text)
        self.assertNotIn("run_id=", self.panel._runtime_label.text)
        self.assertIn("Remaining 12.5s", self.panel._runtime_label.text)
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

    def test_default_ui_text_is_english_and_hides_control_plane_fields(self):
        view = present(
            spec={},
            claims=[],
            workflow={
                "workflow_id": "wf-secret",
                "task_type": "HARDWARE_CONFIGURE",
                "execution_status": "completed",
                "current_stage": "rf_plan_confirmation",
                "stages": [{
                    "id": "rf_plan_confirmation",
                    "label": "射频确认",
                    "execution_status": "completed",
                    "completion": ["rf_plan_approved"],
                }],
            },
            pending={
                "action": "rf_plan_confirmation",
                "purpose": "config_handoff",
                "device": {"type": "pluto", "identity": "usb:test"},
            },
        )
        public_text = " ".join([
            view["phase"]["label"],
            view["specification"]["title"],
            view["workflow"]["title"],
            view["workflow"]["state_label"],
            *[item["label"] for item in view["workflow"]["stages"]],
            view["interaction"]["message"],
            view["interaction"]["confirm_label"],
            view["interaction"]["cancel_label"],
        ]).lower()
        for token in (
            "workflow_id", "task_type", "stage_id", "revision",
            "completion", "intent:", "completed",
        ):
            self.assertNotIn(token, public_text)
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in public_text))

    def test_specification_hides_duplicate_internal_duration_ceiling(self):
        view = present(
            workflow={"shared_intent": {"status": "awaiting_confirmation"}},
            claims=[],
            spec={
                "intent_status": "awaiting_confirmation",
                "radio_specification": [
                    {
                        "key": "duration_seconds",
                        "label": "Maximum duration",
                        "value": 30,
                        "source": "user_text",
                    },
                    {
                        "key": "max_duration_seconds",
                        "label": "Max Duration Seconds",
                        "value": 30,
                        "source": "user_text",
                    },
                    {
                        "key": "hardware",
                        "label": "Device",
                        "value": "plutosdr",
                        "source": "llm",
                    },
                ],
            },
        )
        rows = view["specification"]["rows"]
        self.assertEqual(
            [row["key"] for row in rows],
            ["duration_seconds", "hardware"],
        )
        self.assertEqual(rows[1]["source"], "Extracted")

    def test_alignment_is_visible_before_workflow_creation(self):
        workflow = present(
            workflow={},
            claims=[],
            spec={"intent_status": "awaiting_confirmation"},
        )["workflow"]
        self.assertTrue(workflow["visible"])
        self.assertEqual(workflow["title"], "Radio Specification")
        self.assertEqual(
            workflow["stages"][0]["label"],
            "Awaiting Specification Confirmation",
        )

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
        self.panel._set_pending(interaction_view({}, {
            "action": "stage_recovery",
            "reason": "当前 Stage 未满足完成条件",
            "approved": False,
        }))
        self.assertEqual(self.panel._confirm_btn.label, "Retry This Step")
        self.assertEqual(self.panel._cancel_btn.label, "Cancel Workflow")
        self.assertTrue(self.panel._pending_row.visible)

    def test_open_questions_use_one_compact_line(self):
        details = self.panel._default_details({
            "open_questions": ["Which device?", "How long may it run?"],
        })
        self.assertEqual(
            details,
            "Open questions: Which device? · How long may it run?",
        )
        self.assertNotIn("\n", details)

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
        self.panel._set_pending(interaction_view({}, {
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
        }))
        self.assertEqual(self.panel._confirm_btn.label, "Approve Bounded Transmission")
        self.assertEqual(self.panel._cancel_btn.label, "Cancel")
        self.assertIn("up to 30 seconds", self.panel._pending_label.text)
        self.assertIn("usb:test.pluto", self.panel._pending_label.text)
        self.assertIn("2.402 GHz", self.panel._pending_label.text)
        self.assertIn("2 Msps", self.panel._pending_label.text)
        self.assertIn("Attenuation 30.0 dB", self.panel._pending_label.text)
        self.assertFalse(self.panel._evidence_btn.visible)

        self.panel._set_pending(interaction_view({}, {
            "action": "rf_plan_confirmation",
            "purpose": "config_handoff",
            "device": {"type": "pluto", "identity": "usb:test.pluto"},
            "center_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "bandwidth": 2_000_000.0,
            "tx_attenuation": 30.0,
            "approved": False,
        }))
        self.assertEqual(self.panel._confirm_btn.label, "Confirm Saved Configuration")
        self.assertIn("without starting RF", self.panel._pending_label.text)
        self.assertNotIn("bounded transmission", self.panel._pending_label.text)

        self.panel._set_pending(interaction_view({}, {
            "action": "rf_plan_confirmation",
            "purpose": "device_configuration",
            "requested_effect": "DEVICE_READ",
            "device": {"type": "pluto", "identity": "usb:test.pluto"},
            "center_frequency": 2_402_000_000.0,
            "sample_rate": 2_000_000.0,
            "bandwidth": 2_000_000.0,
            "tx_attenuation": 30.0,
            "approved": False,
        }))
        self.assertEqual(self.panel._confirm_btn.label, "Confirm Configuration")
        self.assertIn("without starting RF", self.panel._pending_label.text)
        self.assertNotIn("bounded transmission", self.panel._pending_label.text)

        self.panel._set_pending(interaction_view({}, {
            "action": "over_air_verification",
            "purpose": "ota_observation",
            "approved": False,
        }))
        self.assertEqual(self.panel._confirm_btn.label, "Target Signal Observed")
        self.assertEqual(self.panel._cancel_btn.label, "Not Observed")
        self.assertTrue(self.panel._evidence_btn.visible)
        self.assertTrue(self.panel._evidence_btn.sensitive)
        self.assertIn("Human confirmation", self.panel._pending_label.text)

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
        self.assertIn("Quality: warning", self.panel._activity_label.text)
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
                "blocking_questions": [
                    {"field": "success_conditions", "prompt": "How will you verify success?"},
                    {"field": "duration_seconds", "prompt": "How long may it run?"},
                ],
                "radio_specification": [
                    {
                        "key": "goal", "label": "Goal", "value": "BLE advertising",
                        "source": "user", "group": "required", "status": "aligned",
                    },
                    {
                        "key": "carrier_frequency", "label": "Carrier",
                        "display_value": "2.402 GHz", "source": "protocol_default",
                        "group": "required", "status": "aligned",
                    },
                    {
                        "key": "success_conditions", "label": "Success condition",
                        "source": "unresolved", "group": "required", "status": "missing",
                    },
                    {
                        "key": "local_name", "label": "Advertising name",
                        "value": "DeepRadio", "source": "extracted",
                        "group": "added", "status": "aligned",
                    },
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
        self.assertEqual(rows["goal"]["group"], "Required")
        self.assertEqual(rows["carrier_frequency"]["group"], "Required")
        self.assertEqual(rows["carrier_frequency"]["source"], "Protocol Default")
        self.assertEqual(rows["success_conditions"]["value"], "?")
        self.assertEqual(rows["success_conditions"]["status"], "missing")
        self.assertEqual(rows["local_name"]["group"], "Added")
        self.assertEqual(rows["local_name"]["source"], "Extracted")
        self.assertEqual(view["specification"]["open_question"], "")
        self.assertFalse(view["specification"]["aligned"])

    def test_workflow_monitor_binds_claims_and_exposes_latest_transition(self):
        view = present(
            spec={"success_conditions": ["接收端观察到信号"]},
            workflow={
                "task_type": "HARDWARE_CONFIGURE",
                "task_label": "配置并运行 SDR",
                "current_stage": "verify",
                "stage_index": 2,
                "stage_total": 3,
                "execution_status": "running",
                "timeline": [
                    {"event": "stage_started", "stage_id": "build"},
                    {
                        "event": "stage_invalidated", "stage_id": "verify",
                        "cause": "规格 revision 使验证结果过期",
                    },
                ],
                "stages": [
                    {"id": "build", "label": "Build", "execution_status": "completed", "outcome": "passed"},
                    {"id": "verify", "label": "Verify", "execution_status": "running"},
                    {"id": "run", "label": "Run", "execution_status": "pending"},
                ],
            },
            claims=[{
                "statement": "PHY valid", "status": "Supported",
                "producer": "verify", "layer": "protocol",
            }],
        )
        monitor = view["workflow"]
        self.assertTrue(monitor["visible"])
        verify = next(item for item in monitor["stages"] if item["id"] == "verify")
        self.assertTrue(verify["current"])
        self.assertEqual(verify["claims"][0]["statement"], "PHY valid")
        self.assertEqual(monitor["transition"]["from"], "Build")
        self.assertEqual(monitor["transition"]["to"], "Verify")
        self.assertIn("revision", monitor["transition"]["reason"])

    def test_stage_row_labels_do_not_collapse_to_one_char_per_line(self):
        """Regression: set_max_width_chars(1)+START made stage text vertical."""
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
        except Exception:  # noqa: BLE001
            self.skipTest("GTK unavailable")
        if not Gtk.init_check()[0]:
            self.skipTest("no display available")

        from grc.gui.ClaimsPanel import ClaimsPanel

        class _Harness:
            _language = "en"
            _t = ClaimsPanel._t
            _build_stage_row = ClaimsPanel._build_stage_row
            _build_stage_claim_line = ClaimsPanel._build_stage_claim_line

        row = _Harness()._build_stage_row({
            "id": "tx_build",
            "label": "Build Transmit Chain",
            "status": "running",
            "current": True,
            "claims": [{
                "statement": "Saved flowgraph passed structural validation",
                "layer": "structure",
                "layer_label": "Flowgraph check",
                "status": "Supported",
            }],
        })
        window = Gtk.OffscreenWindow()
        window.set_size_request(340, 300)
        box = Gtk.VBox()
        box.pack_start(row, False, False, 0)
        window.add(box)
        window.show_all()
        _minimum, natural = row.get_preferred_width()
        self.assertGreater(
            natural, 100,
            "stage row natural width collapsed; text would wrap per character",
        )

    def test_chat_selection_and_resizable_claim_panel(self):
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
        except Exception:  # noqa: BLE001
            self.skipTest("GTK unavailable")
        if not Gtk.init_check()[0]:
            self.skipTest("no display available")

        from grc.gui.AgentPanel import _FlowLabel
        from grc.gui.ClaimsPanel import _stage_label

        self.assertTrue(_FlowLabel().get_selectable())
        self.assertTrue(_stage_label().get_selectable())
        panel = AgentPanel(None)
        self.assertEqual(panel.level_combo.get_active(), 1)
        self.assertEqual(panel.level_combo.get_model().iter_n_children(None), 3)
        self.assertEqual(panel.language_combo.get_active_text(), "EN")
        self.assertEqual(
            panel.language_combo.get_model().iter_n_children(None), 2
        )
        self.assertTrue(
            panel._split.child_get_property(panel.claims_panel, "resize")
        )
        self.assertTrue(
            panel._split.child_get_property(panel.claims_panel, "shrink")
        )
        self.assertTrue(panel.claims_panel._claims_frame.get_vexpand())
        claims_children = (
            panel.claims_panel._claims_frame.get_child().get_children()
        )
        self.assertIs(claims_children[0], panel.claims_panel._claim_summary)
        self.assertIsInstance(claims_children[1], Gtk.ScrolledWindow)

    def test_workflow_header_does_not_repeat_default_title(self):
        try:
            import gi

            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
        except Exception:  # noqa: BLE001
            self.skipTest("GTK unavailable")
        if not Gtk.init_check()[0]:
            self.skipTest("no display available")

        panel = ClaimsPanel()
        panel._set_workflow_monitor({
            "visible": True,
            "title": "Workflow",
            "stage_index": 1,
            "stage_total": 7,
            "state_label": "In progress",
            "stages": [],
        })

        self.assertEqual(
            panel._workflow_monitor.get_label(),
            "Workflow · Step 1/7 · In progress",
        )

    def test_diagnosis_card_only_uses_scoped_findings(self):
        view = present(
            spec={}, claims=[{
                "id": "old-ble", "statement": "old BLE packet passed",
                "layer": "protocol", "status": "Supported",
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

    def test_hardware_detection_exposes_five_states(self):
        workflow = {
            "capabilities": ["hardware_configure"],
            "stages": [{"id": "hardware_preparation", "status": "running"}],
        }
        for state in ("detected", "not_found", "failed"):
            view = present(
                spec={},
                claims=[],
                workflow={
                    **workflow,
                    "hardware_detection": {
                        "state": state,
                        "error": "probe failed" if state == "failed" else "",
                    },
                },
            )
            self.assertEqual(view["hardware_detection"]["state"], state)
        hw = view["hardware_detection"]
        self.assertEqual(hw["label"], "Hardware")
        self.assertEqual(len(hw["rows"]), 1)

        idle = present(spec={}, claims=[], workflow=workflow)
        self.assertEqual(idle["hardware_detection"]["state"], "not_started")

        sim = present(
            spec={},
            claims=[],
            workflow={"task_type": "TX_BUILD", "capabilities": ["build_tx"]},
        )
        self.assertEqual(sim["hardware_detection"]["state"], "not_applicable")

    def test_workflow_keeps_completed_and_pending_groups(self):
        view = present(
            spec={},
            claims=[],
            workflow={
                "task_type": "HARDWARE_CONFIGURE",
                "current_stage": "flowgraph_confirmation",
                "execution_status": "waiting",
                "stages": [
                    {"id": "build_ble_advertiser", "execution_status": "completed", "outcome": "passed"},
                    {"id": "flowgraph_confirmation", "execution_status": "waiting", "outcome": ""},
                    {"id": "hardware_precheck", "execution_status": "pending"},
                ],
            },
        )
        monitor = view["workflow"]
        self.assertEqual([item["id"] for item in monitor["completed"]], ["build_ble_advertiser"])
        self.assertEqual([item["id"] for item in monitor["current"]], ["flowgraph_confirmation"])
        self.assertEqual([item["id"] for item in monitor["pending"]], ["hardware_precheck"])
        self.assertEqual(len(monitor["stages"]), 3)

    def test_workflow_accepts_canonical_stage_status(self):
        monitor = present(
            spec={},
            claims=[],
            workflow={
                "current_stage": "validate",
                "execution_status": "running",
                "stages": [
                    {"id": "design", "status": "completed"},
                    {"id": "validate", "status": "running"},
                    {"id": "deliver", "status": "pending"},
                ],
            },
        )["workflow"]
        self.assertEqual([item["id"] for item in monitor["completed"]], ["design"])
        self.assertEqual([item["id"] for item in monitor["current"]], ["validate"])
        self.assertEqual([item["id"] for item in monitor["pending"]], ["deliver"])

    def test_completed_current_stage_is_not_shown_as_running(self):
        monitor = present(
            spec={},
            claims=[],
            workflow={
                "current_stage": "design",
                "execution_status": "pending",
                "stages": [
                    {"id": "design", "status": "completed"},
                    {"id": "build", "status": "pending"},
                ],
            },
        )["workflow"]
        self.assertEqual([item["id"] for item in monitor["completed"]], ["design"])
        self.assertEqual(monitor["current"], [])
        self.assertEqual([item["id"] for item in monitor["pending"]], ["build"])

    def test_dynamic_rf_permission_keeps_existing_confirmation_ui(self):
        view = interaction_view(
            {
                "wait_kind": "approval",
                "current_stage": "hardware_run",
                "checkpoint_id": "checkpoint-1",
                "checkpoint_purpose": "rf_authorization",
                "requested_effect": "rf.start",
            },
            {
                "action": "hardware_run",
                "checkpoint_id": "checkpoint-1",
                "purpose": "rf_authorization",
                "requested_effect": "rf.start",
            },
        )
        self.assertTrue(view["visible"])
        self.assertEqual(view["confirm_label"], "Approve Bounded Transmission")


if __name__ == "__main__":
    unittest.main()
