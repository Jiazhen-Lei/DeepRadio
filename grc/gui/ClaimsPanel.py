"""Status strip: task, runtime, BLE spec; extras stay collapsed."""

import json
import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GObject, Gtk, Pango

from grc.agent.knowledge.recipes import list_recipes

_MODULATIONS = ("bpsk", "qpsk", "ofdm")
_CHANNELS = ("awgn",)


class ClaimsPanel(Gtk.Frame):
    __gsignals__ = {
        "apply-workflow": (
            GObject.SignalFlags.RUN_FIRST, None, (str, str, str),
        ),
        "confirm-pending": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cancel-pending": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "retry-transmit": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        Gtk.Frame.__init__(self, label="状态")
        self.set_size_request(-1, 120)
        root = Gtk.VBox(spacing=4)
        self.add(root)

        self._claims = []
        self._font_pt = 13
        self._updating = False
        self._recipes = list_recipes()
        self.evidence_path = ""
        self._last_workflow = {}
        self._last_pending = {}
        self._last_spec = {}

        root.pack_start(self._build_activity_bar(), False, False, 2)
        root.pack_start(self._build_spec_bar(), False, False, 0)
        root.pack_start(self._build_metrics_row(), False, False, 0)
        root.pack_start(self._build_workflow_inspector(), False, False, 0)

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
        claims_expander = Gtk.Expander(label="Claims")
        claims_expander.add(scroll)
        self._claims_expander = claims_expander
        root.pack_start(claims_expander, False, False, 0)

        self._hint = Gtk.Label(
            label="描述需求后，这里会显示当前在建图/仿真/诊断哪一步。"
        )
        self._hint.set_line_wrap(True)
        self._hint.set_halign(Gtk.Align.START)
        self._hint.set_margin_start(4)
        root.pack_start(self._hint, False, False, 0)

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
        root.pack_start(self._detail_scroll, False, True, 0)
        self._apply_font()

    def _build_workflow_inspector(self):
        expander = Gtk.Expander(label="执行详情")
        box = Gtk.VBox(spacing=2)
        self._workflow_details = Gtk.TextView()
        self._workflow_details.set_editable(False)
        self._workflow_details.set_cursor_visible(False)
        self._workflow_details.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._workflow_details.set_size_request(-1, 90)
        box.pack_start(self._workflow_details, False, False, 0)

        self._timeline_store = Gtk.ListStore(str, str, str, str)
        view = Gtk.TreeView(model=self._timeline_store)
        view.set_headers_visible(True)
        for index, title in enumerate(("Seq", "Event", "Stage", "Actor")):
            renderer = Gtk.CellRendererText()
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            if index == 1:
                column.set_expand(True)
            view.append_column(column)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(72)
        scroll.add(view)
        box.pack_start(scroll, True, True, 0)
        expander.add(box)
        self._workflow_expander = expander
        self._timeline_expander = expander
        return expander

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

        pending_row = Gtk.HBox(spacing=4)
        self._pending_label = Gtk.Label(label="")
        self._pending_label.set_halign(Gtk.Align.START)
        self._pending_label.set_line_wrap(True)
        self._pending_label.set_hexpand(True)
        pending_row.pack_start(self._pending_label, True, True, 4)
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
        return box

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
        ):
            widget.override_font(small)

    def update_data(self, claims, spec_digest, pending=None,
                    metrics=None, activity=None, workflow=None):
        self._updating = True
        self._claims = list(claims or [])
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
        spec = spec_digest or {}
        self._last_spec = spec
        self._set_combo(self._mod_combo, _MODULATIONS, spec.get("modulation"))
        self._set_combo(self._chan_combo, _CHANNELS, spec.get("channel"))
        recipe_names = [item["name"] for item in self._recipes]
        self._set_combo(self._recipe_combo, recipe_names, spec.get("recipe"))
        self._spec_summary.set_text("规格: " + self._summary_text(spec))
        self._apply_spec_editor_mode(spec)
        self._last_workflow = workflow or {}
        self._set_activity(activity or {}, self._last_workflow)
        self._set_workflow_details(workflow or {})
        self._set_timeline((workflow or {}).get("timeline") or [])
        self._set_metrics(metrics, self._claims)
        pending_view = dict(pending or {})
        workflow_view = workflow or {}
        if not pending_view and workflow_view.get("checkpoint_id"):
            pending_view = {
                "action": workflow_view.get("current_stage") or "workflow_checkpoint",
                "reason": workflow_view.get("waiting_reason") or "继续当前 Workflow",
                "checkpoint_id": workflow_view.get("checkpoint_id"),
                "max_duration_seconds": workflow_view.get("max_duration_seconds"),
                "approved": False,
            }
        pending_view["can_retry"] = bool(
            ((workflow_view.get("runtime") or {}).get("can_retry"))
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
        self._activity_label.set_text("闭环: —  |  当前: 就绪")
        self._set_runtime_line({})
        self.evidence_path = ""
        self._set_metrics({}, [])
        self._set_details("")
        self._set_pending({})
        self._last_spec = {}
        self._set_workflow_details({})
        self._set_timeline([])
        self._hint.set_visible(True)
        self._spec_revealer.set_reveal_child(False)
        if hasattr(self, "_spec_toggle"):
            self._spec_toggle.set_visible(True)
        if hasattr(self, "_workflow_expander"):
            self._workflow_expander.set_expanded(False)
        if hasattr(self, "_claims_expander"):
            self._claims_expander.set_expanded(False)
        self._updating = False

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
                }
                text += "  ·  " + wait_labels.get(wait_kind, wait_kind)
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
            return
        status = str(runtime.get("status") or ("running" if runtime.get("running") else "—"))
        remaining = float(runtime.get("remaining_seconds") or 0.0)
        max_duration = runtime.get("max_duration_seconds") or runtime.get("duration_seconds")
        parts = [
            "发射: {}".format(status),
            "pid={}".format(runtime.get("pid") or "—"),
            "run_id={}".format(runtime.get("run_id") or "—"),
        ]
        if runtime.get("running"):
            parts.append("剩余 {:.1f}s".format(remaining))
        elif runtime.get("return_code") is not None:
            parts.append("return_code={}".format(runtime.get("return_code")))
        if max_duration not in (None, ""):
            parts.append("最大时长 {}s".format(max_duration))
        if runtime.get("running") or runtime.get("do_not_run_grc"):
            parts.append("无需点击 GRC Run")
        text = "  |  ".join(parts)
        tail = str(runtime.get("log_tail") or "").strip()
        if tail:
            last = tail.splitlines()[-1]
            text += "\nlog: " + last
        self._runtime_label.set_text(text)
        self._runtime_label.set_visible(True)

    def _set_workflow_details(self, workflow):
        buffer_ = self._workflow_details.get_buffer()
        if not workflow:
            buffer_.set_text("尚无活动 Workflow")
            return
        lines = [
            "workflow_id={}  revision={}  project_version={}".format(
                workflow.get("workflow_id") or "—",
                workflow.get("revision") or "—",
                workflow.get("base_project_version") or 0,
            )
        ]
        capabilities = workflow.get("capabilities") or []
        if capabilities:
            lines.append("capabilities=" + ", ".join(capabilities))
        blockers = list(workflow.get("missing_slots") or []) + list(
            workflow.get("validation_errors") or []
        )
        if blockers:
            lines.append("blockers=" + ", ".join(blockers))
        interaction = workflow.get("interaction_request") or {}
        if interaction:
            lines.append(
                "interaction={}  reason={}".format(
                    interaction.get("kind") or "—",
                    interaction.get("reason") or "—",
                )
            )
        runtime = workflow.get("runtime") or {}
        if runtime:
            lines.append(
                "runtime={}  run_id={}  pid={}  remaining={:.1f}s  return_code={}".format(
                    runtime.get("status") or "—",
                    runtime.get("run_id") or "—",
                    runtime.get("pid") or "—",
                    float(runtime.get("remaining_seconds") or 0.0),
                    runtime.get("return_code")
                    if runtime.get("return_code") is not None else "—",
                )
            )
            if runtime.get("do_not_run_grc"):
                lines.append("受控进程由 Workflow 管理，无需点击 GRC Run")
            tail = str(runtime.get("log_tail") or "").strip()
            if tail:
                lines.append("runtime.log:")
                lines.extend(tail.splitlines()[-8:])
        for stage in workflow.get("stages") or []:
            marker = "▶" if stage.get("id") == workflow.get("current_stage") else "•"
            completion = stage.get("completion") or []
            results = stage.get("completion_result") or {}
            passed = sum(1 for name in completion if results.get(name) is True)
            outcome = stage.get("outcome") or ""
            status = stage.get("execution_status") or "—"
            if completion:
                all_passed = passed == len(completion) and all(
                    results.get(name) is True for name in completion
                )
                if all_passed:
                    display = "passed"
                elif outcome == "passed":
                    display = "incomplete"
                else:
                    display = outcome or status
            else:
                display = outcome or status
            lines.append(
                "{} {}  {}  attempt {}/{}  completion {}/{}".format(
                    marker,
                    stage.get("label") or stage.get("id") or "—",
                    display,
                    stage.get("attempt") or 0,
                    stage.get("max_attempts") or 1,
                    passed,
                    len(completion),
                )
            )
        spec_extra = self._default_details(getattr(self, "_last_spec", {}) or {})
        if spec_extra:
            lines.append("")
            lines.append(spec_extra)
        buffer_.set_text("\n".join(lines))

    def _set_timeline(self, events):
        if not hasattr(self, "_timeline_store"):
            return
        self._timeline_store.clear()
        for item in events or []:
            self._timeline_store.append(
                [
                    str(item.get("seq") or ""),
                    str(item.get("event") or ""),
                    str(item.get("stage_id") or ""),
                    str(item.get("actor") or ""),
                ]
            )

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
        self.emit("confirm-pending")

    def _on_cancel_pending(self, _button):
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
        self._set_workflow_details(self._last_workflow)
        self._set_timeline(self._last_workflow.get("timeline") or [])
        pending = {
            "action": self._last_workflow.get("current_stage") or "",
            "checkpoint_id": self._last_workflow.get("checkpoint_id") or "",
            "can_retry": bool(
                (self._last_workflow.get("runtime") or {}).get("can_retry")
            ),
            "approved": False,
        }
        if pending["checkpoint_id"]:
            self._set_pending(pending)

    def _set_pending(self, pending):
        pending = pending or {}
        action = str(pending.get("action") or "")
        recipe = str(pending.get("recipe") or "")
        from_recipe = str(pending.get("from_recipe") or "")
        visible = bool(action) and not pending.get("approved")
        if visible:
            if action == "design_link" and recipe:
                text = "待确认: {} → {}".format(
                    from_recipe or "当前工程", recipe)
            elif action == "over_air_verification":
                extra = ""
                if self.evidence_path:
                    extra = "  ·  已选 {}".format(
                        os.path.basename(self.evidence_path)
                    )
                text = (
                    "空口验收: 请确认 LightBlue 实际显示目标广播名称。"
                    "可附加上传截图。{}".format(extra)
                )
            elif action == "rf_plan_confirmation":
                duration = pending.get("max_duration_seconds") or 30
                text = (
                    "RF 安全确认: 批准后将启动最长 {} 秒的受控发射；"
                    "OTA 确认或取消后会提前停止。不要在 GRC 中点击运行。"
                ).format(duration)
            elif action == "workflow_checkpoint":
                text = "待确认: {}".format(
                    pending.get("reason") or "继续当前 Workflow"
                )
            else:
                text = "待确认: {}".format(action)
            self._pending_label.set_text(text)
            if action == "over_air_verification":
                self._confirm_btn.set_label("已看到目标名称")
                self._cancel_btn.set_label("未看到")
            elif action == "rf_plan_confirmation":
                self._confirm_btn.set_label("批准有限时长发射")
                self._cancel_btn.set_label("取消")
            else:
                self._confirm_btn.set_label("确认")
                self._cancel_btn.set_label("取消")
        else:
            self._pending_label.set_text("")
        self._last_pending = pending
        self._pending_row.set_visible(visible)
        self._confirm_btn.set_sensitive(visible)
        self._cancel_btn.set_sensitive(visible)
        ota = visible and action == "over_air_verification"
        self._evidence_btn.set_visible(ota)
        self._evidence_btn.set_sensitive(ota)
        retry = bool(pending.get("can_retry"))
        self._retry_btn.set_visible(retry)
        self._retry_btn.set_sensitive(retry)

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
        wait = str((workflow or {}).get("wait_kind") or "")
        running = bool(((workflow or {}).get("runtime") or {}).get("running"))
        need_exec = wait in ("input", "recovery", "denied") or (
            wait == "approval" and not running
        )
        if need_exec and hasattr(self, "_workflow_expander"):
            self._workflow_expander.set_expanded(True)
        failed = any(
            str(claim.get("status") or "").lower()
            in ("failed", "fail", "inconclusive", "error")
            for claim in claims or []
        )
        if failed and hasattr(self, "_claims_expander"):
            self._claims_expander.set_expanded(True)


def _fmt_metric(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return "{:.3f}".format(number)
