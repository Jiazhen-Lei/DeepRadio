"""Status strip: task, runtime, BLE spec; extras stay collapsed."""

import json
import os

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GObject, Gtk, Pango

from .chat_markup import escape_pango
from .workflow_presenter import layer_label, present

_escape = escape_pango


#: Stage visual styles: completed stages fade to a small muted line, the
#: current stage is emphasized with a bigger bold label on tinted background,
#: upcoming stages stay quiet gray.  ``scale`` is a font-point delta.
_STAGE_STYLES = {
    "passed": {
        "marker": "✓", "marker_color": "#2E9E5B", "text": "#5E6C7E",
        "background": None, "scale": -2, "bold": False,
    },
    "failed": {
        "marker": "✕", "marker_color": "#C43E3E", "text": "#8A2A2A",
        "background": "#FDECEC", "scale": 0, "bold": True,
    },
    "running": {
        "marker": "▶", "marker_color": "#1B62D6", "text": "#1F2933",
        "background": "#E8F0FE", "scale": 1, "bold": True,
    },
    "waiting": {
        "marker": "▶", "marker_color": "#C45B08", "text": "#3D3419",
        "background": "#FFF4E5", "scale": 1, "bold": True,
    },
    "pending": {
        "marker": "○", "marker_color": "#98A2B3", "text": "#98A2B3",
        "background": None, "scale": -1, "bold": False,
    },
}
_STAGE_STYLES["error"] = _STAGE_STYLES["failed"]
_STAGE_STYLES["errored"] = _STAGE_STYLES["failed"]
_STAGE_STYLES["invalidated"] = _STAGE_STYLES["pending"]
# Deferred (planned but not yet scheduled) stages stay visible as quiet
# upcoming steps instead of disappearing from the monitor.
_STAGE_STYLES["deferred"] = dict(_STAGE_STYLES["pending"])

_HW_STATE_STYLES = {
    "not_started": {"marker": "○", "text": "#98A2B3"},
    "detected": {"marker": "✓", "text": "#2E9E5B"},
    "not_found": {"marker": "–", "text": "#C45B08"},
    "failed": {"marker": "✕", "text": "#C43E3E"},
    "not_applicable": {"marker": "·", "text": "#B0B7C3"},
}

#: Current-stage status word shown beside the label.
_STAGE_STATUS_WORDS = {
    "running": "Running…",
    "waiting": "Waiting for you",
    "failed": "Failed",
    "error": "Failed",
    "errored": "Failed",
    "invalidated": "Outdated",
}

#: Claim status colors for the compact evidence lines under a stage.
_CLAIM_STATUS_STYLES = {
    "Passed": ("✓", "#2E9E5B"),
    "Failed": ("✕", "#C43E3E"),
    "Inconclusive": ("?", "#C45B08"),
    "Stale": ("○", "#98A2B3"),
    "Not tested": ("○", "#98A2B3"),
    "Unknown": ("?", "#98A2B3"),
    "Running": ("▶", "#1B62D6"),
    "Pending": ("○", "#98A2B3"),
}


class ClaimsPanel(Gtk.Frame):
    __gsignals__ = {
        "confirm-pending": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "cancel-pending": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "interaction-response": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "retry-transmit": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stop-runtime": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "emergency-stop": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        Gtk.Frame.__init__(self, label="Workflow Monitor")
        self.set_size_request(-1, 280)
        root = Gtk.VBox(spacing=4)
        panel_scroll = Gtk.ScrolledWindow()
        panel_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        panel_scroll.add(root)
        self.add(panel_scroll)

        self._claims = []
        self._font_pt = 13
        self._updating = False
        self.evidence_path = ""
        self._last_workflow = {}
        self._last_pending = {}
        self._last_spec = {}

        root.pack_start(self._build_activity_bar(), False, False, 2)
        root.pack_start(self._build_metrics_row(), False, False, 0)

        self._store = Gtk.ListStore(str, str, str, int)
        self._view = Gtk.TreeView(model=self._store)
        self._view.set_headers_clickable(True)
        self._view.set_enable_search(True)
        self._view.set_search_column(0)
        self._view.set_grid_lines(Gtk.TreeViewGridLines.BOTH)
        self._view.set_tooltip_column(0)
        for index, title in enumerate(("Claim", "Category", "Status", "Version")):
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

        self._workflow_steps = Gtk.VBox(spacing=3)
        self._workflow_monitor = Gtk.Frame(label="Workflow")
        self._workflow_monitor.set_margin_start(2)
        self._workflow_monitor.set_margin_end(2)
        self._workflow_monitor.add(self._workflow_steps)
        root.pack_start(self._workflow_monitor, False, False, 0)

        self._claim_summary = Gtk.Label(label="No verifiable claims yet")
        self._claim_summary.set_halign(Gtk.Align.START)
        self._claim_summary.set_xalign(0.0)
        self._claim_summary.set_margin_start(6)
        claims_expander = Gtk.Expander(label="Claim details")
        claims_body = Gtk.VBox(spacing=2)
        claims_body.pack_start(self._claim_summary, False, False, 0)
        claims_body.pack_start(scroll, False, False, 0)
        claims_expander.add(claims_body)
        self._claims_expander = claims_expander
        self._claims_frame = Gtk.Frame(label="Claims")
        claims_box = Gtk.VBox(spacing=2)
        claims_box.pack_start(claims_expander, False, False, 0)
        self._claims_frame.add(claims_box)
        self._claims_frame.set_margin_start(2)
        self._claims_frame.set_margin_end(2)
        root.pack_start(self._claims_frame, False, False, 0)

        self._hardware_rows = Gtk.VBox(spacing=2)
        self._hardware_frame = Gtk.Frame(label="Hardware Detection")
        self._hardware_frame.set_margin_start(2)
        self._hardware_frame.set_margin_end(2)
        self._hardware_frame.add(self._hardware_rows)
        root.pack_start(self._hardware_frame, False, False, 0)

        self._hint = Gtk.Label(
            label="After you describe a request, the current design, simulation, or diagnosis stage will appear here."
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
        self._interaction_entry.set_placeholder_text("Enter a custom answer")
        self._interaction_entry.set_no_show_all(True)
        self._interaction_entry.set_width_chars(16)
        pending_row.pack_start(self._interaction_entry, False, False, 0)
        self._interaction_choices = []
        self._confirm_btn = Gtk.Button(label="Confirm")
        self._confirm_btn.connect("clicked", self._on_confirm_pending)
        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.connect("clicked", self._on_cancel_pending)
        pending_row.pack_start(self._confirm_btn, False, False, 0)
        pending_row.pack_start(self._cancel_btn, False, False, 2)
        self._evidence_btn = Gtk.Button(label="Attach Screenshot")
        self._evidence_btn.connect("clicked", self._on_attach_evidence)
        self._evidence_btn.set_no_show_all(True)
        pending_row.pack_start(self._evidence_btn, False, False, 0)
        self._retry_btn = Gtk.Button(label="Retry Bounded Transmission")
        self._retry_btn.connect("clicked", self._on_retry_transmit)
        self._retry_btn.set_no_show_all(True)
        pending_row.pack_start(self._retry_btn, False, False, 2)
        # Buttons only exist when there is something to decide; never let a
        # window-wide show_all() reveal an empty confirm/cancel pair.
        pending_row.set_no_show_all(True)
        self._pending_row = pending_row
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

    def set_font_pt(self, pt):
        self._font_pt = max(10, int(pt))
        self._apply_font()

    def _font_desc(self, delta=0):
        desc = Pango.FontDescription()
        desc.set_size(max(9, self._font_pt + delta) * Pango.SCALE)
        return desc

    def _apply_font(self):
        desc = Pango.FontDescription()
        desc.set_size(self._font_pt * Pango.SCALE)
        self._view.override_font(desc)
        self._details.override_font(desc)
        small = Pango.FontDescription()
        small.set_size(max(10, self._font_pt - 1) * Pango.SCALE)
        for widget in (
            self._metrics_label,
            self._pending_label,
            self._hint,
            self._runtime_label,
            self._claim_summary,
        ):
            if widget is not None:
                widget.override_font(small)

    def update_data(self, claims, spec_digest, pending=None,
                    metrics=None, activity=None, workflow=None):
        self._updating = True
        raw_claims = list(claims or [])
        spec = spec_digest or {}
        self._last_spec = spec
        workflow_view = workflow or {}
        view_model = present(
            spec=spec, workflow=workflow_view, claims=raw_claims,
            pending=dict(pending or {}),
        )
        self._claims = list(
            (view_model.get("claims") or {}).get("rows") or []
        )
        self._store.clear()
        for claim in self._claims:
            self._store.append(
                [
                    str(claim.get("statement", "")),
                    layer_label(claim.get("layer", "")),
                    str(claim.get("status", "NotTested")),
                    int(claim.get("project_version", 0)),
                ]
            )
        self._set_paper_view(view_model)
        self._last_workflow = workflow_view
        self._set_activity(activity or {}, self._last_workflow)
        self._set_metrics(metrics, self._claims)
        self._set_pending(dict(view_model.get("interaction") or {}))
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
        self._claim_summary.set_text("No verifiable claims yet")
        self._set_runtime_line({})
        self.evidence_path = ""
        self._set_metrics({}, [])
        self._set_details("")
        self._set_pending({})
        self._last_spec = {}
        self._set_workflow_monitor({})
        self._set_hardware_detection({})
        self._hint.set_visible(True)
        if hasattr(self, "_claims_expander"):
            self._claims_expander.set_expanded(False)
        self._updating = False

    def _set_paper_view(self, view_model):
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
            self._claim_summary.set_text("No verifiable claims yet")
        self._set_workflow_monitor(dict(view_model.get("workflow") or {}))
        self._set_hardware_detection(dict(view_model.get("hardware_detection") or {}))

    def _set_workflow_monitor(self, workflow):
        for child in self._workflow_steps.get_children():
            self._workflow_steps.remove(child)
        if not workflow.get("visible"):
            self._workflow_monitor.set_label("Workflow")
            empty = _stage_label()
            empty.set_markup(
                "<span foreground='#98A2B3'>No active workflow yet</span>"
            )
            empty.set_margin_start(8)
            self._workflow_steps.pack_start(empty, False, False, 4)
            self._workflow_steps.show_all()
            return
        stages = list(workflow.get("stages") or [])
        status = str(workflow.get("state_label") or "Planned")
        self._workflow_monitor.set_label(
            "Workflow · {} · Step {}/{} · {}".format(
                workflow.get("title") or "Workflow",
                workflow.get("stage_index") or 0,
                workflow.get("stage_total") or len(stages),
                status,
            )
        )
        completed = list(workflow.get("completed") or [])
        current = list(workflow.get("current") or [])
        pending = list(workflow.get("pending") or [])
        if not (completed or current or pending):
            current = [item for item in stages if item.get("current")]
            completed = [
                item for item in stages
                if str(item.get("status") or "") in {"passed", "completed"}
                and not item.get("current")
            ]
            pending = [
                item for item in stages
                if item not in completed and item not in current
            ]
        if completed:
            expander = Gtk.Expander(
                label="Completed ({})".format(len(completed))
            )
            expander.set_expanded(False)
            box = Gtk.VBox(spacing=2)
            for stage in completed:
                box.pack_start(self._build_stage_row(stage), False, False, 0)
            expander.add(box)
            self._workflow_steps.pack_start(expander, False, False, 0)
        for stage in current:
            self._workflow_steps.pack_start(
                self._build_stage_row(stage), False, False, 0
            )
        if pending:
            expander = Gtk.Expander(
                label="Upcoming ({})".format(len(pending))
            )
            expander.set_expanded(False)
            box = Gtk.VBox(spacing=2)
            for stage in pending:
                box.pack_start(self._build_stage_row(stage), False, False, 0)
            expander.add(box)
            self._workflow_steps.pack_start(expander, False, False, 0)
        for attempt in workflow.get("previous_workflows") or []:
            self._workflow_steps.pack_start(
                self._build_previous_attempt_line(attempt), False, False, 0
            )
        self._workflow_steps.show_all()

    def _set_hardware_detection(self, detection):
        for child in self._hardware_rows.get_children():
            self._hardware_rows.remove(child)
        state = str((detection or {}).get("state") or "not_started")
        style = _HW_STATE_STYLES.get(state, _HW_STATE_STYLES["not_started"])
        label = _stage_label(wrap=True)
        label.set_markup(
            "<span foreground='{color}'>{state}</span>".format(
                color=style["text"],
                state=_escape(state),
            )
        )
        label.set_margin_start(8)
        label.override_font(self._font_desc(0))
        self._hardware_rows.pack_start(label, False, False, 2)
        error = str((detection or {}).get("error") or "").strip()
        if error and state in {"failed", "not_found"}:
            err = _stage_label(wrap=True)
            err.set_markup(
                "<span foreground='#C43E3E'>{}</span>".format(_escape(error))
            )
            err.set_margin_start(8)
            err.override_font(self._font_desc(-1))
            self._hardware_rows.pack_start(err, False, False, 0)
        self._hardware_rows.show_all()

    def _build_previous_attempt_line(self, attempt):
        """Dim summary of a superseded workflow (Previous Attempt)."""
        status = str(attempt.get("outcome") or attempt.get("status") or "")
        text = "↩ Previous attempt: {}{} · {} stages{}".format(
            attempt.get("task_label") or "Workflow",
            (
                " (stopped at {})".format(attempt.get("stage_label"))
                if attempt.get("stage_label") else ""
            ),
            attempt.get("stage_count") or 0,
            " · {}".format(status) if status else "",
        )
        label = _stage_label()
        label.set_markup("<span foreground='#B0B7C3'>{}</span>".format(
            _escape(text)))
        label.set_margin_top(6)
        label.override_font(self._font_desc(-2))
        return label

    def _build_stage_row(self, stage):
        """One stepper line: marker + label, styled by state, claims nested."""
        stage_status = str(stage.get("status") or "pending")
        is_current = bool(stage.get("current"))
        if is_current:
            style = _STAGE_STYLES.get(
                stage_status if stage_status in ("running", "waiting", "failed")
                else "running"
            )
        else:
            style = _STAGE_STYLES.get(stage_status, _STAGE_STYLES["pending"])
        box = Gtk.VBox(spacing=1)
        box.set_margin_top(3)
        box.set_margin_bottom(3)
        box.set_margin_start(6 if style["background"] else 7)
        box.set_margin_end(7)

        header = _stage_label()
        status_word = _STAGE_STATUS_WORDS.get(stage_status, "") if is_current else (
            "Failed" if style is _STAGE_STYLES["failed"]
            else "Planned" if stage_status == "deferred"
            else ""
        )
        header_text = "{} {}".format(
            style["marker"], stage.get("label") or stage.get("id") or "Stage"
        )
        if status_word:
            header_text += "  ·  {}".format(status_word)
        header.set_markup(
            "<span foreground='{}'{}>{}</span>".format(
                style["text"],
                " weight='bold'" if style["bold"] else "",
                _escape(header_text),
            )
        )
        header.override_font(self._font_desc(style["scale"]))
        box.pack_start(header, False, False, 0)
        for claim in stage.get("claims") or []:
            box.pack_start(
                self._build_stage_claim_line(claim), False, False, 0
            )

        if style["background"]:
            event = Gtk.EventBox()
            color = Gdk.RGBA()
            color.parse(style["background"])
            event.override_background_color(Gtk.StateFlags.NORMAL, color)
            event.add(box)
            return event
        return box

    def _build_stage_claim_line(self, claim):
        status = str(claim.get("status") or "Not tested")
        marker, color = _CLAIM_STATUS_STYLES.get(status, ("○", "#98A2B3"))
        layer = str(claim.get("layer_label") or layer_label(claim.get("layer")))
        label = _stage_label(wrap=True)
        label.set_markup(
            "<span foreground='{color}'>{marker}</span>"
            "<span foreground='#6B7A8C'> {layer} · {statement} · {status}</span>".format(
                color=color,
                marker=_escape(marker),
                layer=_escape(layer),
                statement=_escape(str(claim.get("statement") or "Evidence")),
                status=_escape(status),
            )
        )
        label.set_margin_start(18)
        label.override_font(self._font_desc(-2))
        return label

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
            text = "Task: {}  |  Stage: {} {}/{}  |  Status: {}".format(
                task, stage, index, total, outcome or status or "—"
            )
            wait_kind = str(workflow.get("wait_kind") or "")
            if wait_kind:
                wait_labels = {
                    "approval": "Waiting for approval",
                    "input": "Waiting for input",
                    "recovery": "Waiting for a recovery choice",
                    "denied": "Flowgraph change rejected",
                    "capability": "Required capability not ready",
                    "intent": "Waiting for intent alignment",
                }
                text += "  ·  " + wait_labels.get(wait_kind, wait_kind)
            if quality != "clean":
                text += "  ·  Quality: " + quality
            # Kept as a headless/debug projection for compatibility; the
            # visible panel uses the single Workflow Monitor summary instead.
            if hasattr(self, "_activity_label"):
                self._activity_label.set_text(text)
            self._set_runtime_line(workflow)
            return
        loop = str(activity.get("loop") or "—")
        agent = str(activity.get("agent") or "")
        action = str(activity.get("action") or "Ready")
        status = str(activity.get("status") or "")
        current = " / ".join(part for part in (agent, action) if part)
        text = "Loop: {}  |  Current: {}".format(loop, current or "Ready")
        if status:
            text += "  ·  " + status
        if hasattr(self, "_activity_label"):
            self._activity_label.set_text(text)
        self._set_runtime_line({})

    def _set_runtime_line(self, workflow):
        runtime = (workflow or {}).get("runtime") or {}
        if not runtime or not runtime.get("running"):
            self._runtime_label.set_text("")
            self._runtime_label.set_visible(False)
            if hasattr(self, "_runtime_controls"):
                self._runtime_controls.set_visible(False)
            return
        status = str(runtime.get("status") or ("running" if runtime.get("running") else "—"))
        remaining = float(runtime.get("remaining_seconds") or 0.0)
        max_duration = runtime.get("max_duration_seconds") or runtime.get("duration_seconds")
        parts = [
            "Runtime: {}".format(status),
        ]
        if runtime.get("running"):
            parts.append("Remaining {:.1f}s".format(remaining))
        if max_duration not in (None, ""):
            parts.append("Maximum duration {}s".format(max_duration))
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
                parts.append("Peak {} @ bin {}".format(
                    _fmt_metric(peak), _fmt_metric(peak_bin)))
            else:
                parts.append("Peak {}".format(_fmt_metric(peak)))
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
            "Measurements: " + (" · ".join(parts) if parts else "")
        )

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
            title="Select a LightBlue Screenshot or Capture File",
            parent=toplevel if isinstance(toplevel, Gtk.Window) else None,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Select", Gtk.ResponseType.OK)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images")
        image_filter.add_mime_type("image/png")
        image_filter.add_mime_type("image/jpeg")
        image_filter.add_pattern("*.png")
        image_filter.add_pattern("*.jpg")
        image_filter.add_pattern("*.jpeg")
        dialog.add_filter(image_filter)
        any_filter = Gtk.FileFilter()
        any_filter.set_name("All Files")
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
        visible = bool(pending.get("visible"))
        interaction_combo = getattr(self, "_interaction_combo", None)
        interaction_entry = getattr(self, "_interaction_entry", None)
        if interaction_combo is not None:
            interaction_combo.remove_all()
        if interaction_entry is not None:
            interaction_entry.set_text("")
        self._interaction_choices = []
        ask_intent = False
        if interaction_combo is not None:
            interaction_combo.set_visible(False)
        if interaction_entry is not None:
            interaction_entry.set_visible(False)
        if visible:
            self._pending_label.set_text(str(pending.get("message") or ""))
            self._confirm_btn.set_label(str(pending.get("confirm_label") or "Confirm"))
            self._cancel_btn.set_label(str(pending.get("cancel_label") or "Cancel"))
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
        self._cancel_btn.set_visible(visible)
        ota = visible and bool(pending.get("show_evidence"))
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
    def _default_details(spec):
        lines = []
        questions = [
            str(item).strip()
            for item in spec.get("open_questions") or []
            if str(item).strip()
        ]
        if questions:
            label = "Open question" if len(questions) == 1 else "Open questions"
            lines.append(label + ": " + " · ".join(questions))
        conditions = spec.get("success_conditions") or []
        if conditions:
            lines.append("Success Conditions:\n" + "\n".join(map(str, conditions)))
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
        text = "{}\nCategory: {}\nStatus: {}\nEvidence source: {}".format(
            claim.get("statement", ""),
            layer_label(claim.get("layer", "")),
            claim.get("status", ""),
            claim.get("producer") or "Not available",
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
        copy_cell = Gtk.MenuItem(label="Copy Cell")
        copy_row = Gtk.MenuItem(label="Copy Row")
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


def _stage_label(wrap=False):
    """Stage-row label that never collapses to char-per-line wrapping.

    ``_monitor_label``-style ``set_max_width_chars(1)`` caps the label's own
    allocation at one character, which makes wrapped text render vertically.
    Here headers stay single-line and ellipsize; claim lines wrap on word
    boundaries only when the panel is genuinely narrower than the text.
    """
    label = Gtk.Label()
    label.set_halign(Gtk.Align.START)
    label.set_xalign(0.0)
    if wrap:
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD)
    else:
        label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_hexpand(True)
    return label


def _monitor_label(text):  # retained for compatibility; unused
    label = Gtk.Label(label=str(text or ""))
    label.set_halign(Gtk.Align.START)
    label.set_xalign(0.0)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    label.set_hexpand(True)
    return label
