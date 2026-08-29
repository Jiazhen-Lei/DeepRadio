"""Status strip: task, runtime, BLE spec; extras stay collapsed."""

import json
import os

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GObject, Gtk, Pango

from grc.agent.knowledge.recipes import list_recipes
from .workflow_presenter import present

_MODULATIONS = ("bpsk", "qpsk", "ofdm")
_CHANNELS = ("awgn",)


class ClaimsPanel(Gtk.Frame):
    __gsignals__ = {
        "apply-workflow": (
            GObject.SignalFlags.RUN_FIRST, None, (str, str, str),
        ),
        "confirm-pending": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cancel-pending": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "interaction-response": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "retry-transmit": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stop-runtime": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "emergency-stop": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        Gtk.Frame.__init__(self, label="运行与证据")
        self.set_size_request(-1, 120)
        root = Gtk.VBox(spacing=4)
        panel_scroll = Gtk.ScrolledWindow()
        panel_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        panel_scroll.add(root)
        self.add(panel_scroll)

        self._claims = []
        self._font_pt = 13
        self._updating = False
        self._recipes = list_recipes()
        self.evidence_path = ""
        self._last_workflow = {}
        self._last_pending = {}
        self._last_spec = {}

        self._phase_label = Gtk.Label(label="ALIGN INTENT")
        self._phase_label.set_halign(Gtk.Align.START)
        self._phase_label.set_margin_start(4)
        root.pack_start(self._build_activity_bar(), False, False, 2)
        self._hidden_spec_card = self._build_spec_bar()
        self._hidden_diagnosis_card = self._build_diagnosis_card()
        root.pack_start(self._build_metrics_row(), False, False, 0)

        self._claim_summary = Gtk.Label(label="Claims: 尚无可验证断言")
        self._claim_summary.set_halign(Gtk.Align.START)
        self._claim_summary.set_xalign(0.0)
        self._claim_summary.set_margin_start(4)
        root.pack_start(self._claim_summary, False, False, 0)

        self._store = Gtk.ListStore(str, str, str, int)
        self._view = Gtk.TreeView(model=self._store)
        self._view.set_headers_clickable(True)
        self._view.set_enable_search(True)
        self._view.set_search_column(0)
        self._view.set_grid_lines(Gtk.TreeViewGridLines.BOTH)
        self._view.set_tooltip_column(0)
        for index, title in enumerate(("Claim", "Layer", "Status", "Version")):
            renderer = Gtk.CellRendererText()
            renderer.set_property("editable", False)
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            column.set_sort_column_id(index)
            column.set_reorderable(True)
            if index == 0:
                column.set_expand(True)
                column.set_min_width(80)
            else:
                column.set_min_width(56)
            self._view.append_column(column)
        self._view.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
        self._view.get_selection().connect("changed", self._on_selected)
        self._view.connect("key-press-event", self._on_key_press)
        self._view.connect("button-press-event", self._on_button_press)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(48)
        scroll.add(self._view)
        claims_expander = Gtk.Expander(label="Claims 证据详情")
        claims_expander.add(scroll)
        self._claims_expander = claims_expander
        root.pack_start(claims_expander, False, False, 0)

        self._hint = Gtk.Label(
            label="描述需求后，这里会显示当前在建图/仿真/诊断哪一步。"
        )
        self._hint.set_line_wrap(True)
        self._hint.set_halign(Gtk.Align.START)
        self._hint.set_margin_start(4)

        self._details = Gtk.TextView()
        self._details.set_editable(False)
        self._details.set_cursor_visible(True)
        self._details.set_can_focus(True)
        self._details.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._detail_scroll = Gtk.ScrolledWindow()
        self._detail_scroll.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self._detail_scroll.set_size_request(-1, 48)
        self._detail_scroll.set_no_show_all(True)
        self._detail_scroll.add(self._details)
        self._apply_font()

    def _build_activity_bar(self):
        box = Gtk.VBox(spacing=2)
        self._activity_label = Gtk.Label(label="闭环: —  |  当前: 就绪")
        self._activity_label.set_halign(Gtk.Align.START)
        self._activity_label.set_line_wrap(True)
        self._activity_label.set_xalign(0.0)
        self._activity_label.set_margin_start(4)
        box.pack_start(self._activity_label, False, False, 0)
        self._runtime_label = Gtk.Label(label="")
        self._runtime_label.set_halign(Gtk.Align.START)
        self._runtime_label.set_line_wrap(True)
        self._runtime_label.set_xalign(0.0)
        self._runtime_label.set_margin_start(4)
        self._runtime_label.set_selectable(True)
        self._runtime_label.set_no_show_all(True)
        box.pack_start(self._runtime_label, False, False, 0)

        runtime_controls = Gtk.HBox(spacing=4)
        stop_btn = Gtk.Button(label="Stop")
        stop_btn.connect("clicked", lambda _button: self.emit("stop-runtime"))
        emergency_btn = Gtk.Button(label="Emergency Stop")
        emergency_btn.connect(
            "clicked", lambda _button: self.emit("emergency-stop")
        )
        runtime_controls.pack_start(stop_btn, False, False, 4)
        runtime_controls.pack_start(emergency_btn, False, False, 0)
        runtime_controls.set_no_show_all(True)
        runtime_controls.set_visible(False)
        self._runtime_controls = runtime_controls
        box.pack_start(runtime_controls, False, False, 0)

        pending_row = Gtk.HBox(spacing=4)
        self._pending_label = Gtk.Label(label="")
        self._pending_label.set_halign(Gtk.Align.START)
        self._pending_label.set_line_wrap(True)
        self._pending_label.set_hexpand(True)
        pending_row.pack_start(self._pending_label, True, True, 4)
        self._interaction_combo = Gtk.ComboBoxText()
        self._interaction_combo.set_no_show_all(True)
        pending_row.pack_start(self._interaction_combo, False, False, 0)
        self._interaction_entry = Gtk.Entry()
        self._interaction_entry.set_placeholder_text("填写自定义答案")
        self._interaction_entry.set_no_show_all(True)
        self._interaction_entry.set_width_chars(16)
        pending_row.pack_start(self._interaction_entry, False, False, 0)
        self._interaction_choices = []
        self._confirm_btn = Gtk.Button(label="确认")
        self._confirm_btn.connect("clicked", self._on_confirm_pending)
        self._cancel_btn = Gtk.Button(label="取消")
        self._cancel_btn.connect("clicked", self._on_cancel_pending)
        pending_row.pack_start(self._confirm_btn, False, False, 0)
        pending_row.pack_start(self._cancel_btn, False, False, 2)
        self._evidence_btn = Gtk.Button(label="附加上传截图")
        self._evidence_btn.connect("clicked", self._on_attach_evidence)
        self._evidence_btn.set_no_show_all(True)
        pending_row.pack_start(self._evidence_btn, False, False, 0)
        self._retry_btn = Gtk.Button(label="受控重试发射")
        self._retry_btn.connect("clicked", self._on_retry_transmit)
        self._retry_btn.set_no_show_all(True)
        pending_row.pack_start(self._retry_btn, False, False, 2)
        self._pending_row = pending_row
        box.pack_start(pending_row, False, False, 0)
        self._set_pending({})
        return box

    def _build_metrics_row(self):
        self._metrics_label = Gtk.Label(label="")
        self._metrics_label.set_halign(Gtk.Align.START)
        self._metrics_label.set_line_wrap(True)
        self._metrics_label.set_xalign(0.0)
        self._metrics_label.set_margin_start(4)
        self._metrics_label.set_no_show_all(True)
        return self._metrics_label

    def _build_spec_bar(self):
        box = Gtk.VBox(spacing=2)
        row = Gtk.HBox(spacing=4)
        self._spec_summary = Gtk.Label(label="规格: 尚未提取")
        self._spec_summary.set_halign(Gtk.Align.START)
        self._spec_summary.set_line_wrap(True)
        self._spec_summary.set_hexpand(True)
        self._spec_summary.set_xalign(0.0)
        row.pack_start(self._spec_summary, True, True, 4)
        self._spec_toggle = Gtk.Button(label="改规格")
        self._spec_toggle.connect("clicked", self._on_toggle_spec)
        self._spec_toggle.set_no_show_all(True)
        self._spec_toggle.set_visible(True)
        row.pack_start(self._spec_toggle, False, False, 2)
        box.pack_start(row, False, False, 0)

        self._spec_rows = Gtk.Label(label="")
        self._spec_rows.set_halign(Gtk.Align.START)
        self._spec_rows.set_xalign(0.0)
        self._spec_rows.set_line_wrap(True)
        self._spec_rows.set_selectable(True)
        self._spec_rows.set_margin_start(6)
        self._spec_rows.set_margin_end(6)
        box.pack_start(self._spec_rows, False, False, 2)

        editor = Gtk.HBox(spacing=4)
        self._mod_combo = Gtk.ComboBoxText()
        self._mod_combo.append_text("(调制)")
        for name in _MODULATIONS:
            self._mod_combo.append_text(name.upper())
        self._mod_combo.set_active(0)
        editor.pack_start(self._mod_combo, True, True, 0)
        editor.pack_start(Gtk.Label(label="→"), False, False, 0)

        self._chan_combo = Gtk.ComboBoxText()
        self._chan_combo.append_text("(信道)")
        for name in _CHANNELS:
            self._chan_combo.append_text(name.upper())
        self._chan_combo.set_active(0)
        editor.pack_start(self._chan_combo, True, True, 0)
        editor.pack_start(Gtk.Label(label="→"), False, False, 0)

        self._recipe_combo = Gtk.ComboBoxText()
        self._recipe_combo.append_text("(配方)")
        for item in self._recipes:
            self._recipe_combo.append_text(item["name"])
        self._recipe_combo.set_active(0)
        editor.pack_start(self._recipe_combo, True, True, 0)

        apply_btn = Gtk.Button(label="写入规格")
        apply_btn.connect("clicked", self._on_apply_clicked)
        editor.pack_start(apply_btn, False, False, 2)

        self._spec_revealer = Gtk.Revealer()
        self._spec_revealer.set_reveal_child(False)
        self._spec_revealer.add(editor)
        box.pack_start(self._spec_revealer, False, False, 0)
        frame = Gtk.Frame(label="Radio Specification")
        frame.add(box)
        return frame

    def _build_diagnosis_card(self):
        frame = Gtk.Frame(label="Diagnosis")
        self._diagnosis_label = Gtk.Label(label="")
        self._diagnosis_label.set_halign(Gtk.Align.START)
        self._diagnosis_label.set_xalign(0.0)
        self._diagnosis_label.set_line_wrap(True)
        self._diagnosis_label.set_selectable(True)
        self._diagnosis_label.set_margin_start(6)
        self._diagnosis_label.set_margin_end(6)
        frame.add(self._diagnosis_label)
        frame.set_no_show_all(True)
        frame.set_visible(False)
        self._diagnosis_frame = frame
        return frame

    def set_font_pt(self, pt):
        self._font_pt = max(10, int(pt))
        self._apply_font()

    def _apply_font(self):
        desc = Pango.FontDescription()
        desc.set_size(self._font_pt * Pango.SCALE)
        self._view.override_font(desc)
        self._details.override_font(desc)
        small = Pango.FontDescription()
        small.set_size(max(10, self._font_pt - 1) * Pango.SCALE)
        for widget in (
            self._activity_label,
            self._metrics_label,
            self._spec_summary,
            self._pending_label,
            self._hint,
            self._runtime_label,
            self._phase_label,
            self._spec_rows,
            self._diagnosis_label,
            self._claim_summary,
        ):
            widget.override_font(small)

    def update_data(self, claims, spec_digest, pending=None,
                    metrics=None, activity=None, workflow=None):
        self._updating = True
        raw_claims = list(claims or [])
        spec = spec_digest or {}
        self._last_spec = spec
        workflow_view = workflow or {}
        view_model = present(
            spec=spec, workflow=workflow_view, claims=raw_claims
        )
        self._claims = list(
            (view_model.get("claims") or {}).get("details") or []
        )
        self._store.clear()
        for claim in self._claims:
            self._store.append(
                [
                    str(claim.get("statement", "")),
                    str(claim.get("layer", "")),
                    str(claim.get("status", "NotTested")),
                    int(claim.get("project_version", 0)),
                ]
            )
        self._set_paper_view(view_model)
        self._set_combo(self._mod_combo, _MODULATIONS, spec.get("modulation"))
        self._set_combo(self._chan_combo, _CHANNELS, spec.get("channel"))
        recipe_names = [item["name"] for item in self._recipes]
        self._set_combo(self._recipe_combo, recipe_names, spec.get("recipe"))
        self._spec_summary.set_text("规格: " + self._summary_text(spec))
        self._apply_spec_editor_mode(spec)
        self._last_workflow = workflow_view
        self._set_activity(activity or {}, self._last_workflow)
        self._set_metrics(metrics, self._claims)
        pending_view = dict(pending or {})
        wait = str(workflow_view.get("wait_kind") or "")
        if pending_view.get("action") == "intent_alignment":
            # Intent choices live in the conversation Radio Specification card.
            pending_view = {}
        elif wait == "approval":
            pending_view.setdefault(
                "requested_effect", workflow_view.get("requested_effect") or ""
            )
            pending_view.setdefault(
                "purpose", workflow_view.get("checkpoint_purpose") or ""
            )
            if not pending_view.get("checkpoint_id") and workflow_view.get("checkpoint_id"):
                pending_view = {
                    "action": workflow_view.get("current_stage") or "workflow_checkpoint",
                    "reason": workflow_view.get("waiting_reason") or "继续当前 Workflow",
                    "checkpoint_id": workflow_view.get("checkpoint_id"),
                    "requested_effect": workflow_view.get("requested_effect") or "",
                    "purpose": workflow_view.get("checkpoint_purpose") or "",
                    "approved": False,
                }
        elif wait == "recovery":
            pending_view = pending_view or {
                "action": "stage_recovery",
                "reason": workflow_view.get("waiting_reason") or "当前 Stage 未通过",
                "approved": False,
            }
        elif wait == "capability":
            blocker = dict(workflow_view.get("blocker") or {})
            pending_view = pending_view or {
                "action": "capability_blocker",
                "reason": blocker.get("message")
                or workflow_view.get("waiting_reason")
                or "当前系统能力未就绪",
                "blocker": blocker,
                "can_confirm": False,
                "can_retry": bool(blocker.get("retryable", False)),
                "approved": False,
            }
        elif wait != "intent":
            pending_view = {}
        if pending_view:
            pending_view["can_retry"] = bool(
                pending_view.get("can_retry")
                or ((workflow_view.get("runtime") or {}).get("can_retry"))
            )
        self._set_pending(pending_view)
        self._sync_expanders(workflow_view, self._claims)
        empty = (
            not self._claims
            and not spec.get("recipe")
            and not spec.get("modulation")
            and not spec.get("protocol")
        )
        self._hint.set_visible(empty)
        if empty or not self._view.get_selection().get_selected()[1]:
            self._set_details("")
        self._updating = False

    def clear(self):
        self._updating = True
        self._claims = []
        self._store.clear()
        self._set_combo(self._mod_combo, _MODULATIONS, "")
        self._set_combo(self._chan_combo, _CHANNELS, "")
        recipe_names = [item["name"] for item in self._recipes]
        self._set_combo(self._recipe_combo, recipe_names, "")
        self._spec_summary.set_text("规格: 尚未提取")
        self._spec_rows.set_text("")
        self._phase_label.set_text("ALIGN INTENT")
        self._diagnosis_label.set_text("")
        self._diagnosis_frame.set_visible(False)
        self._claim_summary.set_text("Claims: 尚无可验证断言")
        self._activity_label.set_text("闭环: —  |  当前: 就绪")
        self._set_runtime_line({})
        self.evidence_path = ""
        self._set_metrics({}, [])
        self._set_details("")
        self._set_pending({})
        self._last_spec = {}
        self._hint.set_visible(True)
        self._spec_revealer.set_reveal_child(False)
        if hasattr(self, "_spec_toggle"):
            self._spec_toggle.set_visible(True)
        if hasattr(self, "_claims_expander"):
            self._claims_expander.set_expanded(False)
        self._updating = False

    def _set_paper_view(self, view_model):
        phase = dict(view_model.get("phase") or {})
        phase_id = str(phase.get("id") or "align_intent")
        phase_color = {
            "align_intent": "#4B3F8F",
            "co_construct": "#C45B08",
            "verify_operate": "#3C7A49",
        }.get(phase_id, "#4B3F8F")
        self._phase_label.set_markup(
            "<b><span foreground='{}'>{}</span></b>".format(
                phase_color, phase.get("label") or "ALIGN INTENT"
            )
        )

        specification = dict(view_model.get("specification") or {})
        lines = []
        for row in specification.get("rows") or []:
            lines.append("{:<18} {}  [{}]".format(
                str(row.get("label") or ""),
                str(row.get("value") or "?"),
                str(row.get("source") or "System"),
            ))
        if specification.get("aligned"):
            lines.append("✓ Specification aligned")
        self._spec_rows.set_text("\n".join(lines))

        diagnosis = dict(view_model.get("diagnosis") or {})
        diagnosis_lines = []
        status_icon = {
            "pass": "✓", "passed": "✓", "fail": "✕", "failed": "✕",
            "unknown": "?", "stale": "○",
        }
        for finding in diagnosis.get("findings") or []:
            status_id = str(finding.get("status_id") or "unknown")
            line = "{} {} — {}".format(
                status_icon.get(status_id, "•"),
                finding.get("label") or finding.get("id") or "Check",
                finding.get("status") or "Unknown",
            )
            if finding.get("observation"):
                line += "\n   " + str(finding["observation"])
            if finding.get("remediation") and status_id != "pass":
                line += "\n   建议: " + str(finding["remediation"])
            diagnosis_lines.append(line)
        self._diagnosis_label.set_text("\n".join(diagnosis_lines))
        self._diagnosis_frame.set_visible(bool(diagnosis.get("visible")))

        claim_view = dict(view_model.get("claims") or {})
        counts = dict(claim_view.get("counts") or {})
        if counts:
            ordered = ("Failed", "Inconclusive", "Stale", "Not tested", "Passed")
            summary = " · ".join(
                "{} {}".format(name, counts[name])
                for name in ordered if counts.get(name)
            )
            self._claim_summary.set_text("Claims: " + summary)
        else:
            self._claim_summary.set_text("Claims: 尚无可验证断言")

    def _set_activity(self, activity, workflow=None):
        workflow = workflow or {}
        if workflow:
            task = str(
                workflow.get("task_label") or workflow.get("task_type") or "—"
            )
            stage = str(
                workflow.get("stage_label") or workflow.get("current_stage") or "—"
            )
            index = workflow.get("stage_index") or 0
            total = workflow.get("stage_total") or 0
            status = str(workflow.get("execution_status") or "")
            outcome = str(workflow.get("outcome") or "")
            quality = str(workflow.get("quality") or "clean")
            text = "任务: {}  |  阶段: {} {}/{}  |  状态: {}".format(
                task, stage, index, total, outcome or status or "—"
            )
            wait_kind = str(workflow.get("wait_kind") or "")
            if wait_kind:
                wait_labels = {
                    "approval": "等待批准",
                    "input": "等待补充",
                    "recovery": "等待恢复选择",
                    "denied": "改图被拒绝",
                    "capability": "系统能力未就绪",
                    "intent": "等待意图对齐",
                }
                text += "  ·  " + wait_labels.get(wait_kind, wait_kind)
            if quality != "clean":
                text += "  ·  质量: " + quality
            self._activity_label.set_text(text)
            self._set_runtime_line(workflow)
            return
        loop = str(activity.get("loop") or "—")
        agent = str(activity.get("agent") or "")
        action = str(activity.get("action") or "就绪")
        status = str(activity.get("status") or "")
        current = " / ".join(part for part in (agent, action) if part)
        text = "闭环: {}  |  当前: {}".format(loop, current or "就绪")
        if status:
            text += "  ·  " + status
        self._activity_label.set_text(text)
        self._set_runtime_line({})

    def _set_runtime_line(self, workflow):
        runtime = (workflow or {}).get("runtime") or {}
        if not runtime:
            self._runtime_label.set_text("")
            self._runtime_label.set_visible(False)
            if hasattr(self, "_runtime_controls"):
                self._runtime_controls.set_visible(False)
            return
        status = str(runtime.get("status") or ("running" if runtime.get("running") else "—"))
        remaining = float(runtime.get("remaining_seconds") or 0.0)
        max_duration = runtime.get("max_duration_seconds") or runtime.get("duration_seconds")
        parts = [
            "运行: {}".format(status),
        ]
        if runtime.get("run_id"):
            parts.append("run_id={}".format(runtime.get("run_id")))
        if runtime.get("running"):
            parts.append("剩余 {:.1f}s".format(remaining))
        if max_duration not in (None, ""):
            parts.append("最大时长 {}s".format(max_duration))
        text = "  |  ".join(parts)
        self._runtime_label.set_text(text)
        self._runtime_label.set_visible(True)
        if hasattr(self, "_runtime_controls"):
            self._runtime_controls.set_visible(bool(runtime.get("running")))

    def _set_metrics(self, metrics, _claims):
        parts = []
        metrics = metrics or {}
        if metrics.get("evm_pct") is not None:
            parts.append("EVM {}".format(_fmt_metric(metrics.get("evm_pct"))) + "%")
        elif metrics.get("evm") is not None:
            parts.append("EVM {}".format(_fmt_metric(metrics.get("evm"))))
        if metrics.get("ber") is not None:
            parts.append("BER {}".format(_fmt_metric(metrics.get("ber"))))
        peak = metrics.get("spectrum_peak")
        peak_bin = metrics.get("spectrum_peak_bin")
        if peak is not None:
            if peak_bin is not None:
                parts.append("主峰 {} @ bin {}".format(
                    _fmt_metric(peak), _fmt_metric(peak_bin)))
            else:
                parts.append("主峰 {}".format(_fmt_metric(peak)))
        leftover = []
        for key, value in metrics.items():
            if key in (
                "evm_pct", "evm", "ber", "spectrum_peak", "spectrum_peak_bin",
                "n_symbols",
            ):
                continue
            leftover.append("{}={}".format(key, _fmt_metric(value)))
        parts.extend(leftover[:3])
        visible = bool(parts)
        self._metrics_label.set_visible(visible)
        self._metrics_label.set_text(
            "测量: " + (" · ".join(parts) if parts else "")
        )

    @staticmethod
    def _set_combo(combo, values, current):
        needle = str(current or "").strip().lower()
        if not needle:
            combo.set_active(0)
            return
        for index, name in enumerate(values, start=1):
            if name.lower() == needle:
                combo.set_active(index)
                return
        combo.set_active(0)

    def _combo_value(self, combo, values):
        idx = combo.get_active()
        if idx <= 0:
            return ""
        if idx > len(values):
            return ""
        return values[idx - 1]

    def _on_toggle_spec(self, _button):
        self._spec_revealer.set_reveal_child(
            not self._spec_revealer.get_reveal_child()
        )

    def _on_apply_clicked(self, _button):
        recipe_names = [item["name"] for item in self._recipes]
        modulation = self._combo_value(self._mod_combo, _MODULATIONS)
        channel = self._combo_value(self._chan_combo, _CHANNELS)
        recipe = self._combo_value(self._recipe_combo, recipe_names)
        self.emit("apply-workflow", modulation, channel, recipe)
        self._spec_revealer.set_reveal_child(False)

    def _on_confirm_pending(self, _button):
        if str((self._last_pending or {}).get("action") or "") == "intent_alignment":
            pending = dict(self._last_pending or {})
            payload = {
                "action": "interaction_response",
                "interaction_id": pending.get("interaction_id"),
                "base_intent_revision": pending.get("base_intent_revision"),
            }
            if pending.get("kind") == "intent_confirmation":
                payload["decision"] = "approved"
            else:
                index = self._interaction_combo.get_active()
                if 0 <= index < len(self._interaction_choices):
                    payload["value"] = self._interaction_choices[index].get("value")
                custom = self._interaction_entry.get_text().strip()
                if custom:
                    payload["custom_value"] = custom
                if payload.get("value") in (None, "") and not custom:
                    self._interaction_entry.grab_focus()
                    return
            self.emit("interaction-response", json.dumps(payload, ensure_ascii=False))
            return
        self.emit("confirm-pending")

    def _on_cancel_pending(self, _button):
        if str((self._last_pending or {}).get("action") or "") == "intent_alignment":
            pending = dict(self._last_pending or {})
            if pending.get("kind") == "intent_confirmation":
                payload = {
                    "action": "interaction_response",
                    "interaction_id": pending.get("interaction_id"),
                    "base_intent_revision": pending.get("base_intent_revision"),
                    "decision": "revise",
                }
                self.emit("interaction-response", json.dumps(payload, ensure_ascii=False))
            return
        self.emit("cancel-pending")

    def _on_retry_transmit(self, _button):
        self.emit("retry-transmit")

    def _on_attach_evidence(self, _button):
        toplevel = self.get_toplevel()
        dialog = Gtk.FileChooserDialog(
            title="选择 LightBlue 截图或抓包文件",
            parent=toplevel if isinstance(toplevel, Gtk.Window) else None,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("选择", Gtk.ResponseType.OK)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("图片")
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        image_filter.add_pattern("*.png")
        image_filter.add_pattern("*.jpg")
        image_filter.add_pattern("*.jpeg")
        dialog.add_filter(image_filter)
        any_filter = Gtk.FileFilter()
        any_filter.set_name("所有文件")
        any_filter.add_pattern("*")
        dialog.add_filter(any_filter)
        try:
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                self.evidence_path = dialog.get_filename() or ""
                if getattr(self, "_last_pending", None):
                    self._set_pending(self._last_pending)
        finally:
            dialog.destroy()

    def refresh_runtime(self, workflow):
        self._last_workflow = workflow or {}
        self._set_activity({}, self._last_workflow)
        wait = str(self._last_workflow.get("wait_kind") or "")
        if wait == "approval" and self._last_workflow.get("checkpoint_id"):
            pending = dict(self._last_pending or {})
            pending.update({
                "action": self._last_workflow.get("current_stage") or pending.get("action") or "",
                "checkpoint_id": self._last_workflow.get("checkpoint_id"),
                "requested_effect": (
                    self._last_workflow.get("requested_effect")
                    or pending.get("requested_effect")
                    or ""
                ),
                "purpose": (
                    self._last_workflow.get("checkpoint_purpose")
                    or pending.get("purpose")
                    or ""
                ),
                "can_retry": bool(
                    (self._last_workflow.get("runtime") or {}).get("can_retry")
                ),
                "approved": False,
            })
            self._set_pending(pending)
        elif wait not in ("recovery", "capability"):
            self._set_pending({})

    def _set_pending(self, pending):
        pending = pending or {}
        action = str(pending.get("action") or "")
        purpose = str(pending.get("purpose") or "")
        if not purpose and str(pending.get("requested_effect") or "") == "RF_RUN":
            purpose = "rf_authorization"
        recipe = str(pending.get("recipe") or "")
        from_recipe = str(pending.get("from_recipe") or "")
        visible = bool(action) and not pending.get("approved")
        interaction_combo = getattr(self, "_interaction_combo", None)
        interaction_entry = getattr(self, "_interaction_entry", None)
        if interaction_combo is not None:
            interaction_combo.remove_all()
        if interaction_entry is not None:
            interaction_entry.set_text("")
        self._interaction_choices = list(pending.get("choices") or [])
        for choice in self._interaction_choices:
            if interaction_combo is not None:
                interaction_combo.append_text(str(choice.get("label") or choice.get("id") or "选项"))
        if self._interaction_choices and interaction_combo is not None:
            interaction_combo.set_active(0)
        is_intent = visible and action == "intent_alignment"
        ask_intent = is_intent and pending.get("kind") == "ask_user_question"
        if interaction_combo is not None:
            interaction_combo.set_visible(ask_intent and bool(self._interaction_choices))
        if interaction_entry is not None:
            interaction_entry.set_visible(ask_intent and bool(pending.get("allow_custom")))
        if visible:
            if action == "intent_alignment":
                text = str(pending.get("prompt") or pending.get("reason") or "请补充意图信息。")
                if pending.get("kind") == "intent_confirmation" and pending.get("summary"):
                    text += "  " + str(pending.get("summary"))
            elif action == "design_link" and recipe:
                text = "待确认: {} → {}".format(
                    from_recipe or "当前工程", recipe)
            elif action == "over_air_verification":
                extra = ""
                if self.evidence_path:
                    extra = "  ·  已选 {}".format(
                        os.path.basename(self.evidence_path)
                    )
                else:
                    extra = " 人工确认、附件缺失。"
                text = (
                    "空口验收: 请确认 LightBlue 实际显示目标广播名称。"
                    "可附加上传截图。{}".format(extra)
                )
            elif action == "rf_plan_confirmation":
                duration = pending.get("max_duration_seconds")
                device = dict(pending.get("device") or {})
                identity = str(device.get("identity") or "未绑定")
                device_type = str(device.get("type") or "SDR")
                frequency = self._format_si(
                    pending.get("center_frequency"), "Hz"
                )
                sample_rate = self._format_si(
                    pending.get("sample_rate"), "sps"
                )
                bandwidth = self._format_si(pending.get("bandwidth"), "Hz")
                level = (
                    "衰减 {} dB".format(pending.get("tx_attenuation"))
                    if pending.get("tx_attenuation") is not None
                    else "增益 {} dB".format(pending.get("tx_gain"))
                    if pending.get("tx_gain") is not None
                    else "功率参数未设置"
                )
                if purpose == "rf_authorization":
                    text = (
                        "RF 安全确认: {} [{}] · {} · {} · BW {} · {}。"
                        "批准后将启动最长 {} 秒的受控发射；"
                        "OTA 确认或取消后会提前停止。不要在 GRC 中点击运行。"
                    ).format(
                        device_type, identity, frequency or "频率?",
                        sample_rate or "采样率?", bandwidth or "?", level,
                        duration or 30,
                    )
                else:
                    text = (
                        "配置确认: {} [{}] · {} · {} · BW {} · {}。"
                        "确认后不启动射频。若要发射，请明确授权运行。"
                    ).format(
                        device_type, identity, frequency or "频率?",
                        sample_rate or "采样率?", bandwidth or "?", level,
                    )
            elif action == "stage_recovery":
                text = "Stage 未通过: {}".format(
                    pending.get("reason") or "可重试本阶段或取消任务"
                )
            elif action == "capability_blocker":
                blocker = dict(pending.get("blocker") or {})
                text = "系统能力未就绪: {}".format(
                    pending.get("reason") or "当前操作不可执行"
                )
                if blocker.get("remediation"):
                    text += "  " + str(blocker["remediation"])
            elif action == "workflow_checkpoint":
                text = "待确认: {}".format(
                    pending.get("reason") or "继续当前 Workflow"
                )
            else:
                text = "待确认: {}".format(action)
            self._pending_label.set_text(text)
            if action == "intent_alignment":
                if pending.get("kind") == "intent_confirmation":
                    self._confirm_btn.set_label("确认并建立 Workflow")
                    self._cancel_btn.set_label("继续修改")
                else:
                    self._confirm_btn.set_label("提交答案")
                    self._cancel_btn.set_label("")
            elif action == "over_air_verification":
                self._confirm_btn.set_label("已看到目标名称")
                self._cancel_btn.set_label("未看到")
            elif action == "rf_plan_confirmation":
                if purpose == "rf_authorization":
                    self._confirm_btn.set_label("批准有限时长发射")
                elif purpose == "config_handoff":
                    self._confirm_btn.set_label("确认已保存")
                else:
                    self._confirm_btn.set_label("确认配置")
                self._cancel_btn.set_label("取消")
            elif action == "stage_recovery":
                self._confirm_btn.set_label("重试本阶段")
                self._cancel_btn.set_label("取消任务")
            elif action == "capability_blocker":
                self._confirm_btn.set_label("当前进程不可确认")
                self._cancel_btn.set_label("取消任务")
            else:
                self._confirm_btn.set_label("确认")
                self._cancel_btn.set_label("取消")
        else:
            self._pending_label.set_text("")
            self._confirm_btn.set_label("")
            self._cancel_btn.set_label("")
        self._last_pending = pending
        self._pending_row.set_visible(visible)
        can_confirm = bool(pending.get("can_confirm", True))
        self._confirm_btn.set_visible(visible and can_confirm)
        self._confirm_btn.set_sensitive(visible and can_confirm)
        self._cancel_btn.set_sensitive(visible)
        self._cancel_btn.set_visible(
            visible and not (ask_intent and pending.get("kind") != "intent_confirmation")
        )
        ota = visible and action == "over_air_verification"
        self._evidence_btn.set_visible(ota)
        self._evidence_btn.set_sensitive(ota)
        retry = bool(pending.get("can_retry"))
        self._retry_btn.set_visible(retry)
        self._retry_btn.set_sensitive(retry)

    @staticmethod
    def _format_si(value, unit):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        for scale, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
            if abs(number) >= scale:
                return "{:g} {}{}".format(number / scale, suffix, unit)
        return "{:g} {}".format(number, unit)

    @staticmethod
    def _summary_text(spec):
        parts = []
        summary = str(spec.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        else:
            modulation = str(spec.get("modulation") or "").upper() or "?"
            channel = str(spec.get("channel") or "").upper() or "?"
            recipe = str(spec.get("recipe") or "") or "?"
            parts.append("{} → {} → {}".format(modulation, channel, recipe))
        duration_note = str(spec.get("duration_note") or "").strip()
        if duration_note:
            parts.append(duration_note)
        return "  |  ".join(parts)

    @staticmethod
    def _default_details(spec):
        lines = []
        questions = spec.get("open_questions") or []
        if questions:
            lines.append("待澄清:\n" + "\n".join(questions))
        conditions = spec.get("success_conditions") or []
        if conditions:
            lines.append("成功条件:\n" + "\n".join(map(str, conditions)))
        return "\n\n".join(lines)

    def _on_selected(self, selection):
        if getattr(self, "_updating", False):
            return
        model, iterator = selection.get_selected()
        if iterator is None:
            self._set_details("")
            return
        index = model.get_path(iterator).get_indices()[0]
        if index >= len(self._claims):
            return
        claim = self._claims[index]
        evidence = claim.get("evidence") or []
        text = "{}\nLayer: {}\nStatus: {}\nVersion: {}\n{}".format(
            claim.get("statement", ""),
            claim.get("layer", ""),
            claim.get("status", ""),
            claim.get("project_version", 0),
            json.dumps(evidence, ensure_ascii=False, indent=2),
        )
        self._set_details(text)

    def _on_key_press(self, _view, event):
        if event.keyval not in (Gdk.KEY_c, Gdk.KEY_C):
            return False
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if mods & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.META_MASK
            | Gdk.ModifierType.MOD1_MASK
        ):
            self._copy_selected_row()
            return True
        return False

    def _on_button_press(self, view, event):
        if event.button != 3:
            return False
        path_info = view.get_path_at_pos(int(event.x), int(event.y))
        if path_info is None:
            return False
        path, column, _x, _y = path_info
        view.grab_focus()
        view.set_cursor(path, column, False)
        menu = Gtk.Menu()
        copy_cell = Gtk.MenuItem(label="复制此单元格")
        copy_row = Gtk.MenuItem(label="复制整行")
        copy_cell.connect(
            "activate", lambda *_: self._copy_cell(path, column))
        copy_row.connect("activate", lambda *_: self._copy_selected_row())
        menu.append(copy_cell)
        menu.append(copy_row)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _copy_cell(self, path, column):
        columns = list(self._view.get_columns())
        try:
            index = columns.index(column)
        except ValueError:
            return
        iterator = self._store.get_iter(path)
        value = self._store.get_value(iterator, index)
        self._set_clipboard(str(value))

    def _copy_selected_row(self):
        model, iterator = self._view.get_selection().get_selected()
        if iterator is None:
            return
        values = [str(model.get_value(iterator, i)) for i in range(4)]
        self._set_clipboard("\t".join(values))

    @staticmethod
    def _set_clipboard(text):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text or "", -1)
        clipboard.store()

    def _set_details(self, text):
        self._details.get_buffer().set_text(text or "")
        visible = bool(text)
        if hasattr(self, "_detail_scroll"):
            self._detail_scroll.set_visible(visible)

    @staticmethod
    def _is_ble_spec(spec):
        text = " ".join(
            str(spec.get(key) or "")
            for key in ("spec_kind", "protocol", "summary", "recipe")
        ).lower()
        return "ble" in text

    def _apply_spec_editor_mode(self, spec):
        ble = self._is_ble_spec(spec)
        if hasattr(self, "_spec_toggle"):
            self._spec_toggle.set_visible(not ble)
        if ble and hasattr(self, "_spec_revealer"):
            self._spec_revealer.set_reveal_child(False)

    def _sync_expanders(self, workflow, claims):
        # Expanding evidence is always a deliberate user action.
        del workflow, claims


def _fmt_metric(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return "{:.3f}".format(number)
