"""Read-only Claims, Evidence, and Spec view for AgentPanel."""

import json

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk


class ClaimsPanel(Gtk.Frame):
    def __init__(self):
        Gtk.Frame.__init__(self, label="Claims / Evidence")
        self.set_size_request(-1, 180)
        root = Gtk.VBox(spacing=4)
        self.add(root)

        self._claims = []
        self._store = Gtk.ListStore(str, str, str, int)
        view = Gtk.TreeView(model=self._store)
        for index, title in enumerate(("Claim", "Layer", "Status", "Version")):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=index)
            column.set_resizable(True)
            view.append_column(column)
        self._updating = False
        view.get_selection().connect("changed", self._on_selected)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(view)
        root.pack_start(scroll, True, True, 0)

        self._details = Gtk.TextView()
        self._details.set_editable(False)
        self._details.set_cursor_visible(False)
        self._details.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        detail_scroll = Gtk.ScrolledWindow()
        detail_scroll.set_policy(
            Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC
        )
        detail_scroll.set_size_request(-1, 72)
        detail_scroll.add(self._details)
        root.pack_start(detail_scroll, False, True, 0)

    def update_data(self, claims, spec_digest):
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
        lines = []
        if spec.get("goals"):
            lines.append("Goals: " + "; ".join(map(str, spec["goals"])))
        if spec.get("decisions"):
            decisions = [
                "{}={} ({})".format(
                    item.get("key"), item.get("value"), item.get("source")
                )
                for item in spec["decisions"]
            ]
            lines.append("Decisions: " + "; ".join(decisions))
        if spec.get("open_questions"):
            lines.append(
                "Open questions: " + "; ".join(spec["open_questions"])
            )
        self._set_details("\n".join(lines))
        self._updating = False

    def clear(self):
        self._updating = True
        self._claims = []
        self._store.clear()
        self._set_details("")
        self._updating = False

    def _on_selected(self, selection):
        if getattr(self, "_updating", False):
            return
        model, iterator = selection.get_selected()
        if iterator is None:
            return
        index = model.get_path(iterator).get_indices()[0]
        if index >= len(self._claims):
            return
        claim = self._claims[index]
        evidence = claim.get("evidence") or []
        text = "{}\n{}".format(
            claim.get("statement", ""),
            json.dumps(evidence, ensure_ascii=False, indent=2),
        )
        self._set_details(text)

    def _set_details(self, text):
        self._details.get_buffer().set_text(text or "")
