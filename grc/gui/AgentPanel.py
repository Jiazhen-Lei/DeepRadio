"""
Agent 右侧对话面板 (多轮协商版 GTK 界面)。

在 MainWindow 右侧提供一个人机对话面板:

* **多轮协商 DeepRadio**(默认): 持有 ``ServiceAgent``，按闭环模式委派多个领域
  Subagent，并通过 SharedState 展示可追溯 Spec 与 Claim/Evidence；
  交付 .grc 时 emit ``open_flow_graph`` 让 MainWindow **原地刷新**当前画布。
* **专业度档位**(创新 B): 下拉可选 自适应 / 小白 / 学生 / 专家; 选具体档位则
  钉档 (pin), 选"自适应"则放开 (unpin) 让画像随对话自适应。

所有产物统一输出到工程根目录下的 ``local/output/``, 便于查找与管理。

SPDX-License-Identifier: GPL-2.0-or-later
"""

import json
import logging
import math
import os
import threading
import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, GObject, Gdk, GdkPixbuf, Pango

from .ClaimsPanel import ClaimsPanel
from .chat_markup import escape_pango, markdown_to_pango
from .workflow_presenter import present

log = logging.getLogger(__name__)


def _project_root():
    """定位工程根目录 (本文件在 grc/gui/ 下, 向上两级)。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir, os.pardir))


def _output_dir():
    """统一产物输出目录: <工程根>/local/output/, 不存在则创建。"""
    out = os.path.join(_project_root(), "local", "output")
    os.makedirs(out, exist_ok=True)
    return out


#: 档位下拉项 -> (是否自适应, 钉档档位值)。档位值对应 ExpertiseLevel。
_LEVEL_CHOICES = [
    ("Adaptive", (True, None)),
    ("Novice", (False, "novice")),
    ("Student", (False, "student")),
    ("Expert", (False, "expert")),
]

#: 产物图字段 -> 中文标题, 按此顺序内联展示。
_ARTIFACT_IMAGES = [
    ("constellation_png", "Constellation"),
    ("spectrum_png", "Spectrum"),
    ("eye_png", "Eye Diagram"),
]

_USER_ROLES = ("You",)
_BUBBLE_RADIUS = 16

# Quartz 上 CSS background-color 经常不生效,气泡底色用 Cairo 画。
_THEME = {
    "user": {
        "fill": "#2F6FED",
        "border": None,
        "text": "#FFFFFF",
        "caption": "#5B6B82",
    },
    "agent": {
        "fill": "#FFFFFF",
        "border": "#D0D5DD",
        "text": "#1F2933",
        "caption": "#5B6B82",
    },
    "meta": {
        "fill": "#FFF6D8",
        "border": "#E8D48B",
        "text": "#3D3419",
        "caption": "#5B6B82",
    },
}
_CHAT_BG = "#EEF1F6"

_ACTIVITY_BY_TOOL = {
    "design_link": ("Build", "Flowgraph"),
    "simulate": ("Simulate", "Verification"),
    "run_simulation": ("Simulate", "Verification"),
    "plot_spectrum": ("Plot", "Verification"),
    "plot_constellation": ("Plot", "Verification"),
    "plot_eye": ("Plot", "Verification"),
    "read_metric": ("Measure", "Verification"),
    "debug_by_metric": ("Diagnose", "Diagnosis"),
    "diagnose_by_metric": ("Diagnose", "Diagnosis"),
    "verify_claims": ("Verify claims", "Verification"),
    "apply_grc_diff": ("Update parameters", "Flowgraph"),
    "recipe_switch_propose": ("Await confirmation", "Flowgraph"),
    "select_recipe": ("Select design", "RadioDesign"),
    "spec_commit": ("Extract specification", "Spec"),
    "spec_clarify": ("Clarify intent", "Spec"),
}
_LOOP_BY_STAGE = {
    "CONFIRM": "Revise",
    "DELIVER": "Deliver",
    "DENY": "Denied",
    "CANCELLED": "Cancelled",
    "CRITIC": "Verify",
    "ERROR": "Error",
}


def _activity_from_reply(reply):
    stage = getattr(reply, "stage", "") or ""
    pending = getattr(reply, "pending", None) or {}
    if pending and not pending.get("approved"):
        action = str(pending.get("action") or "")
        if action == "rf_plan_confirmation":
            rf_grant = (
                str(pending.get("purpose") or "") == "rf_authorization"
                or (
                    not pending.get("purpose")
                    and str(pending.get("requested_effect") or "")
                    in {"RF_RUN", "rf.start"}
                )
            )
            return {
                "loop": "Confirm",
                "agent": "Hardware",
                "action": "Confirm RF plan" if rf_grant else "Confirm configuration",
                "status": "Awaiting explicit authorization" if rf_grant else "Confirm configuration without RF transmission",
            }
        return {
            "loop": "Revise",
            "agent": "Flowgraph",
            "action": "Await confirmation",
            "status": "The flowgraph changes only after confirmation",
        }
    last = ""
    for item in reversed(getattr(reply, "tool_invocations", None) or []):
        name = getattr(item, "name", "") or ""
        if name and name not in ("reply", "user_input"):
            last = name
            break
    action, agent = _ACTIVITY_BY_TOOL.get(last, ("", ""))
    status = {
        "CONFIRM": "Awaiting your confirmation",
        "DELIVER": "Completed",
        "DENY": "Denied",
        "CANCELLED": "Cancelled",
        "ERROR": "Error",
        "CRITIC": "Verification failed",
    }.get(stage, "")
    return {
        "loop": _LOOP_BY_STAGE.get(stage, "Execute"),
        "agent": agent or "Orchestrator",
        "action": action or stage or "Ready",
        "status": status,
    }


def _parse_rgba(hex_color):
    color = Gdk.RGBA()
    color.parse(hex_color)
    return color


def _rounded_rect(cr, width, height, radius):
    radius = min(float(radius), width / 2.0, height / 2.0)
    cr.new_path()
    cr.arc(width - radius, radius, radius, -math.pi / 2.0, 0)
    cr.arc(width - radius, height - radius, radius, 0, math.pi / 2.0)
    cr.arc(radius, height - radius, radius, math.pi / 2.0, math.pi)
    cr.arc(radius, radius, radius, math.pi, 3.0 * math.pi / 2.0)
    cr.close_path()


class _CaptionLabel(Gtk.Label):
    """角色名(DeepRadio / 我)单行显示,绝不拆开换行。"""

    def __init__(self):
        Gtk.Label.__init__(self)
        self.set_line_wrap(False)
        self.set_single_line_mode(True)
        self.set_ellipsize(Pango.EllipsizeMode.NONE)
        self.set_hexpand(False)
        self.set_xalign(0.0)
        self._dr_role = "caption"


class _FlowLabel(Gtk.Label):
    """对话正文:按已分配宽度换行,不把侧栏撑宽。"""

    __gtype_name__ = "DeepRadioFlowLabel"

    def __init__(self, **kwargs):
        Gtk.Label.__init__(self, **kwargs)
        self.set_line_wrap(True)
        self.set_line_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.set_justify(Gtk.Justification.LEFT)
        self.set_xalign(0.0)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_max_width_chars(1)
        self.set_width_chars(1)
        self._dr_role = "body"

    def do_get_preferred_width(self):
        return 1, 1

    def do_get_preferred_width_for_height(self, _height):
        return 1, 1

    def do_get_preferred_height(self):
        alloc = self.get_allocation()
        width = alloc.width if alloc.width > 1 else 240
        return self.do_get_preferred_height_for_width(width)

    def do_get_preferred_height_for_width(self, width):
        width = max(int(width), 1)
        layout = self.get_layout()
        if layout is None:
            return Gtk.Label.do_get_preferred_height_for_width(self, width)
        layout.set_width(width * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        _ink, logical = layout.get_pixel_extents()
        extra = self.get_margin_top() + self.get_margin_bottom()
        height = max(1, int(logical.height) + extra)
        return height, height


class _ChatBubble(Gtk.Box):
    """圆角色块气泡:用 draw 信号铺底,宽度跟随侧栏。"""

    __gtype_name__ = "DeepRadioChatBubble"

    def __init__(self, fill_hex, border_hex=None, radius=_BUBBLE_RADIUS):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self._fill = _parse_rgba(fill_hex)
        self._border = _parse_rgba(border_hex) if border_hex else None
        self._radius = radius
        self.set_app_paintable(True)
        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.connect("draw", self._on_draw)

    def do_get_preferred_width(self):
        return 1, 1

    def do_get_preferred_width_for_height(self, _height):
        return 1, 1

    def do_get_preferred_height_for_width(self, width):
        return Gtk.Box.do_get_preferred_height_for_width(
            self, max(int(width), 1))

    def _on_draw(self, widget, cr):
        alloc = widget.get_allocation()
        width = float(alloc.width)
        height = float(alloc.height)
        if width <= 0 or height <= 0:
            return False
        _rounded_rect(cr, width, height, self._radius)
        Gdk.cairo_set_source_rgba(cr, self._fill)
        if self._border is not None:
            cr.fill_preserve()
            Gdk.cairo_set_source_rgba(cr, self._border)
            cr.set_line_width(1.0)
            cr.stroke()
        else:
            cr.fill()
        return False


class AgentPanel(Gtk.VBox):
    """右侧 DeepRadio 对话面板 (多轮协商 + 档位 + 内联产物图)。

    对外发出信号:
        open_flow_graph(str): 请求 MainWindow 把给定路径的 .grc 刷到当前页。
    """

    __gsignals__ = {
        'open_flow_graph': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, platform):
        Gtk.VBox.__init__(self)
        log.debug("AgentPanel()")

        # 复用 GUI 现成的 platform, 不再 make_platform() 自建块库。
        self.platform = platform
        self._busy = False
        self._out_dir = _output_dir()
        self._runtime_poll_id = None

        # 多轮协商 Agent 实例 (惰性创建, 避免无 gnuradio 时导入报错)。
        self._agent = None
        self._canvas_path = ""
        # ---- 顶部控制条 ----
        ctrl = Gtk.HBox()
        ctrl.pack_start(Gtk.Label(label="Expertise:"), False, False, 2)

        self.level_combo = Gtk.ComboBoxText()
        for label, _ in _LEVEL_CHOICES:
            self.level_combo.append_text(label)
        self.level_combo.set_active(0)  # 默认"自适应"
        self.level_combo.connect('changed', self._on_level_changed)
        ctrl.pack_start(self.level_combo, False, False, 2)

        self.pack_start(ctrl, expand=False, fill=True, padding=2)

        # ---- 聊天历史显示区: 用 VBox 容纳文本气泡 + 内联图片 ----
        self._log_box = Gtk.VBox()
        self._log_box.set_spacing(10)
        self._log_box.set_margin_top(10)
        self._log_box.set_margin_bottom(10)
        self._log_box.set_margin_start(6)
        self._log_box.set_margin_end(6)
        self._log_box.set_hexpand(True)
        self._log_box.set_halign(Gtk.Align.FILL)
        self._log_box.set_valign(Gtk.Align.START)
        chat_bg = Gtk.EventBox()
        chat_bg.set_hexpand(True)
        chat_bg.set_halign(Gtk.Align.FILL)
        chat_bg.override_background_color(
            Gtk.StateFlags.NORMAL, _parse_rgba(_CHAT_BG))
        chat_bg.add(self._log_box)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        if hasattr(scroll, "set_propagate_natural_width"):
            scroll.set_propagate_natural_width(False)
        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        viewport.set_hexpand(True)
        viewport.set_halign(Gtk.Align.FILL)
        viewport.add(chat_bg)
        scroll.add(viewport)
        self._scroll = scroll
        self._chat_width = 0
        self._live_cards = []
        scroll.connect("size-allocate", self._on_chat_size_allocate)
        self.claims_panel = ClaimsPanel()
        self.claims_panel.connect("confirm-pending", self._on_confirm_pending)
        self.claims_panel.connect("cancel-pending", self._on_cancel_pending)
        self.claims_panel.connect("retry-transmit", self._on_retry_transmit)
        self.claims_panel.connect("stop-runtime", self._on_stop_runtime)
        self.claims_panel.connect("emergency-stop", self._on_emergency_stop)
        self.claims_panel.connect(
            "interaction-response", self._on_interaction_response
        )
        split = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        if hasattr(split, "set_wide_handle"):
            split.set_wide_handle(True)
        split.pack1(scroll, True, False)
        split.pack2(self.claims_panel, False, False)
        split.connect("size-allocate", self._on_split_allocate)
        self._split = split
        self._split_inited = False
        self.pack_start(split, expand=True, fill=True, padding=0)
        self.pack_start(self.claims_panel._pending_row, expand=False, fill=True, padding=2)

        # ---- 输入区: 文本框 + 发送按钮 ----
        input_box = Gtk.HBox()
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(
            "Describe the radio system you want, e.g. build a BPSK link over AWGN and show the constellation")
        self.entry.connect('activate', self._on_send)
        input_box.pack_start(self.entry, expand=True, fill=True, padding=0)

        self.send_button = Gtk.Button(label="Send")
        self.send_button.connect('clicked', self._on_send)
        input_box.pack_start(self.send_button, expand=False, fill=False,
                             padding=0)

        self.pack_start(input_box, expand=False, fill=False, padding=2)

        self.set_hexpand(True)
        self.set_halign(Gtk.Align.FILL)
        self.set_size_request(260, -1)
        self._append("DeepRadio",
                     "Hello! I can help design and operate a radio system through iterative collaboration "
                     "(flowgraph → simulation → refinement).\nArtifacts are saved under "
                     "local/output/<session_id>/ and session records under local/agent_sessions/. "
                     "Describe what you want to accomplish.")

    # ------------------------------------------------------------------ #
    # Agent 惰性创建
    # ------------------------------------------------------------------ #
    def _ensure_agent(self):
        """首次交互时创建 Agent, 并把当前档位/输出目录同步进去。

        主 Agent 走 deepagents 深度代理(service.ServiceAgent);未装 deepagents
        或未配置 LLM 时, ServiceAgent 内部自动降级到确定性 design_link 建图。
        """
        if self._agent is None:
            from grc.agent.service import build_service_agent
            self._agent = build_service_agent()
            if hasattr(self._agent, "subscribe_progress"):
                self._agent.subscribe_progress(self._on_agent_progress_from_worker)
            # 必须用 core Platform(env.make_platform),不能复用 GUI Platform.
            # design_link 在后台线程跑 FlowGraph.update,GUI 块带 Pango/Cairo,
            # 与主线程画布抢同一套 GTK 对象会在 macOS 上 malloc abort.
            # 产物统一落到 local/output/ (通过 ctx 兼容层)。
            self._agent.ctx.tool_ctx.out_dir = self._out_dir
            # 把当前下拉档位应用到画像。
            self._apply_level_to_agent()
            if self._canvas_path:
                try:
                    self._agent.bind_opened_project(self._canvas_path)
                except Exception as exc:  # noqa: BLE001
                    log.warning("绑定画布工程失败: %s", exc)
        return self._agent

    def _on_confirm_pending(self, _panel):
        self._submit_checkpoint_decision("approved")

    def _on_cancel_pending(self, _panel):
        self._submit_checkpoint_decision("rejected")

    def _on_retry_transmit(self, _panel):
        if self._busy:
            return
        self._append("You", "Retry Bounded Transmission")
        self._set_busy(True)
        threading.Thread(
            target=self._handle_agent_command,
            args=({"action": "retry_transmit"},),
            daemon=True,
        ).start()

    def _on_stop_runtime(self, _panel):
        self._submit_runtime_stop(emergency=False)

    def _on_emergency_stop(self, _panel):
        self._submit_runtime_stop(emergency=True)

    def _submit_runtime_stop(self, *, emergency):
        if self._busy or self._agent is None:
            return
        self._append("You", "Emergency Stop" if emergency else "Stop Runtime")
        self._set_busy(True)
        threading.Thread(
            target=self._handle_agent_command,
            args=({"action": "emergency_stop" if emergency else "stop_runtime"},),
            daemon=True,
        ).start()

    def _on_interaction_response(self, _panel, payload):
        if self._busy:
            return
        try:
            command = json.loads(payload or "{}")
        except (TypeError, ValueError):
            self._append("DeepRadio", "Invalid intent response. Please try again.")
            return
        label = "Confirm Intent" if command.get("decision") == "approved" else (
            "Continue Revising Intent" if command.get("decision") == "revise" else
            str(command.get("custom_value") or command.get("value") or "Submit Answer")
        )
        self._append("You", label)
        self._set_busy(True)
        threading.Thread(
            target=self._handle_agent_command, args=(command,), daemon=True
        ).start()

    def _submit_checkpoint_decision(self, decision):
        if self._busy:
            return
        agent = self._ensure_agent()
        digest = agent._workflow.digest()
        wait_kind = str(digest.get("wait_kind") or "")
        if wait_kind == "capability":
            blocker = dict(digest.get("blocker") or {})
            self._append(
                "DeepRadio",
                "{}{}".format(
                    blocker.get("message") or "The required system capability is not ready.",
                    "\n" + str(blocker.get("remediation") or "")
                    if blocker.get("remediation") else "",
                ),
            )
            return
        if wait_kind == "recovery":
            action = (
                "retry_stage" if decision == "approved" else "cancel_workflow"
            )
            self._append(
                "You",
                "Retry This Stage" if action == "retry_stage" else "Cancel Task",
            )
            self._set_busy(True)
            threading.Thread(
                target=self._handle_agent_command,
                args=({"action": action},),
                daemon=True,
            ).start()
            return
        checkpoint_id = str(digest.get("checkpoint_id") or "")
        if not checkpoint_id:
            self._append("DeepRadio", "There is no pending checkpoint.")
            return
        current_stage = str(digest.get("current_stage") or "")
        purpose = str(digest.get("checkpoint_purpose") or "")
        is_ota = purpose == "ota_observation" or current_stage == "over_air_verification"
        self._append(
            "You",
            ("Target Signal Observed" if is_ota else "Confirm")
            if decision == "approved" else "Not Observed / Cancel",
        )
        self._set_busy(True)
        command = {
            "action": "checkpoint_decision",
            "checkpoint_id": checkpoint_id,
            "decision": decision,
        }
        if is_ota:
            slots = agent._workflow.workflow.intent.slots
            artifact = str(getattr(self.claims_panel, "evidence_path", "") or "")
            command["observation"] = {
                "observed_name": str(slots.get("local_name") or ""),
                "observed_at": time.time(),
                "evidence_kind": "screenshot" if artifact else "human_confirmation",
                "artifact": artifact,
            }
        threading.Thread(
            target=self._handle_agent_command, args=(command,), daemon=True
        ).start()

    def _handle_agent_command(self, command):
        try:
            reply = self._ensure_agent().step_command(command)
            GLib.idle_add(self._on_agent_reply, reply)
        except Exception as exc:  # noqa: BLE001
            log.exception("Agent command 失败")
            GLib.idle_add(self._on_error, str(exc))

    def notify_canvas_saved(self, file_path):
        """画布保存 session 工程后，把版本与 Claim 标脏。"""
        if self._agent is None:
            return
        try:
            result = self._agent.sync_from_canvas(file_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("canvas 逆同步失败: %s", exc)
            return
        if not result.get("ok"):
            return
        self.claims_panel.update_data(
            result.get("claims") or [],
            result.get("spec_digest") or {},
            activity={
                "loop": "Revise",
                "agent": "Flowgraph",
                "action": "Canvas Saved",
                "status": "Claims Need Revalidation",
            },
            workflow=result.get("workflow_digest") or {},
        )
        self._replace_interaction_cards(
            result.get("spec_digest") or {},
            result.get("workflow_digest") or {},
            result.get("claims") or [],
            {},
        )
        self._set_status(
            "Canvas saved as project version {}; claims need revalidation.".format(
                result.get("version", "?")))

    def notify_canvas_opened(self, file_path):
        """File→Open / 打开最近文件后，把当前画布绑定为 current_project。"""
        path = os.path.abspath(file_path or "")
        if not path or not os.path.isfile(path) or not path.endswith(".grc"):
            return
        self._canvas_path = path
        if self._agent is None:
            return
        try:
            self._agent.bind_opened_project(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("绑定打开的流图失败: %s", exc)

    def notify_canvas_cleared(self):
        self._canvas_path = ""
        if self._agent is None:
            return
        try:
            self._agent.clear_opened_project()
        except Exception as exc:  # noqa: BLE001
            log.warning("清除画布工程失败: %s", exc)

    def _apply_level_to_agent(self):
        """把档位下拉的选择应用到 Agent 的 profile / adaptive 开关。"""
        if self._agent is None:
            return
        idx = self.level_combo.get_active()
        if idx < 0:
            idx = 0
        adaptive, pinned = _LEVEL_CHOICES[idx][1]
        if hasattr(self._agent, "record_profile_choice"):
            self._agent.record_profile_choice(adaptive=adaptive, pinned=pinned)
            return
        ctx = self._agent.ctx
        ctx.adaptive = adaptive
        if pinned is None:
            ctx.profile.unpin()
        else:
            ctx.profile.pin(pinned)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_level_changed(self, _combo):
        self._apply_level_to_agent()
        idx = self.level_combo.get_active()
        label = _LEVEL_CHOICES[max(idx, 0)][0]
        self._set_status("Expertise: {}".format(label))

    def _on_split_allocate(self, paned, allocation):
        if self._split_inited or allocation.height < 240:
            return
        self._split_inited = True
        paned.set_position(max(160, allocation.height - 150))

    def _on_chat_size_allocate(self, _widget, allocation):
        width = int(allocation.width)
        if width < 80:
            return
        if abs(width - self._chat_width) < 4 and self._chat_width:
            return
        self._chat_width = width
        self._constrain_chat_bodies(self._log_box, self._body_wrap_width())
        self._log_box.queue_resize()

    def _body_wrap_width(self):
        return max(80, int(self._chat_width) - 48)

    def _constrain_chat_bodies(self, widget, body_w):
        """只锁正文宽度;角色名保持自然单行。"""
        role = getattr(widget, "_dr_role", None)
        if role == "body":
            widget.set_size_request(body_w, -1)
        elif role == "caption":
            widget.set_size_request(-1, -1)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._constrain_chat_bodies(child, body_w)

    def _on_send(self, _widget):
        if self._busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text('')
        self._submit_agent_text(text)

    def _submit_agent_text(self, text, echo=True):
        if self._busy or not text:
            return
        if echo:
            self._append("You", text)
        self._set_busy(True)
        threading.Thread(target=self._handle_agent,
                         args=(text,), daemon=True).start()

    # -- 多轮协商 Agent 路径 --------------------------------------------- #
    def _handle_agent(self, text):
        """子线程: 走 agent.step 多轮协商。UI 更新回主线程。"""
        try:
            agent = self._ensure_agent()
            if self._canvas_path:
                agent.bind_opened_project(self._canvas_path)
            reply = agent.step(text)
            GLib.idle_add(self._on_agent_reply, reply)
        except Exception as e:  # noqa: BLE001
            log.exception("Agent step 失败")
            GLib.idle_add(self._on_error, str(e))

    def _on_agent_reply(self, reply):
        """主线程: 回显叙述 + 内联产物图；仅交付阶段刷新画布。"""
        self._append("DeepRadio", reply.text or "(No output)")

        artifacts = reply.artifacts or {}
        # 内联展示产物图。
        for key, title in _ARTIFACT_IMAGES:
            path = artifacts.get(key)
            if path and os.path.exists(path):
                self._append_image(title, path)
        metrics = artifacts.get("metrics") if isinstance(
            artifacts.get("metrics"), dict) else {}
        self.claims_panel.update_data(
            getattr(reply, "claims", []),
            getattr(reply, "spec_digest", {}),
            pending=getattr(reply, "pending", None) or {},
            metrics=metrics,
            activity=_activity_from_reply(reply),
            workflow=getattr(reply, "workflow_digest", None) or {},
        )
        self._replace_interaction_cards(
            getattr(reply, "spec_digest", {}) or {},
            getattr(reply, "workflow_digest", None) or {},
            getattr(reply, "claims", []) or [],
            getattr(reply, "pending", None) or {},
        )
        # 交付与确认都刷画布；DENY/CANCELLED 不覆盖用户当前图。
        stage = getattr(reply, "stage", "") or ""
        skip_canvas = stage in ("DENY", "CANCELLED")
        grc_path = artifacts.get("grc_path") or artifacts.get("path")
        if (not skip_canvas) and grc_path and str(grc_path).endswith(".grc") \
                and os.path.exists(grc_path):
            self.emit('open_flow_graph', grc_path)
            canvas_note = "Generated {}; the canvas has been refreshed".format(
                os.path.basename(grc_path))
        else:
            canvas_note = ""

        tip = " (Waiting for your confirmation or response)" if getattr(reply, "needs_confirmation",
                                            False) else ""
        level = self._agent.ctx.profile.level if self._agent else "?"
        digest = getattr(reply, "workflow_digest", None) or {}
        workflow_note = ""
        if digest:
            workflow_note = "Task: {} | Stage: {}/{} {}".format(
                digest.get("task_label") or digest.get("task_type") or "?",
                digest.get("stage_index") or 0,
                digest.get("stage_total") or 0,
                digest.get("stage_label") or digest.get("current_stage") or "—",
            )
        parts = [p for p in (
            canvas_note,
            workflow_note,
            "Phase: {} | Expertise: {}{}".format(stage, level, tip),
        ) if p]
        self._set_status(" | ".join(parts))
        self._set_busy(False)
        self._schedule_runtime_poll(digest)
        return False

    def _on_agent_progress_from_worker(self, snapshot):
        """Service callback may run on an executor thread; marshal into GTK."""
        GLib.idle_add(self._on_agent_progress, dict(snapshot or {}))

    def _on_agent_progress(self, snapshot):
        workflow = dict(snapshot.get("workflow_digest") or {})
        self.claims_panel.update_data(
            snapshot.get("claims") or [],
            snapshot.get("spec_digest") or {},
            pending=snapshot.get("pending") or {},
            activity={
                "loop": "Execute",
                "agent": "Workflow",
                "action": str(snapshot.get("event") or "State Updated"),
                "status": str(workflow.get("execution_status") or ""),
            },
            workflow=workflow,
        )
        return False

    def _schedule_runtime_poll(self, digest):
        running = bool((digest or {}).get("runtime", {}).get("running"))
        if running and self._runtime_poll_id is None:
            self._runtime_poll_id = GLib.timeout_add(1000, self._on_runtime_poll)
        if not running:
            self._stop_runtime_poll()

    def _stop_runtime_poll(self):
        if self._runtime_poll_id is not None:
            GLib.source_remove(self._runtime_poll_id)
            self._runtime_poll_id = None

    def _on_runtime_poll(self):
        if self._agent is None:
            return True
        try:
            digest = self._agent.peek_runtime_digest()
        except Exception as exc:  # noqa: BLE001
            log.debug("runtime poll failed: %s", exc)
            return True
        self.claims_panel.refresh_runtime(digest)
        running = bool((digest.get("runtime") or {}).get("running"))
        if not running and not self._busy:
            self._runtime_poll_id = None
            return False
        return True

    def _on_error(self, message):
        self._append("DeepRadio", "Error: {}".format(message))
        self._set_status("Error")
        self._set_busy(False)
        self._stop_runtime_poll()
        return False

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt(v):
        try:
            return "{:.3f}".format(float(v))
        except (TypeError, ValueError):
            return str(v)

    def _set_busy(self, busy):
        self._busy = busy
        self.entry.set_sensitive(not busy)
        self.send_button.set_sensitive(not busy)
        self.level_combo.set_sensitive(not busy)
        self.send_button.set_label("Processing…" if busy else "Send")
        if busy and self._runtime_poll_id is None:
            self._runtime_poll_id = GLib.timeout_add(1000, self._on_runtime_poll)

    def _set_status(self, text):
        # User-facing state is rendered as conversation cards.  Keep this
        # compatibility hook for existing call sites without creating a
        # second status line above the input box.
        log.debug("DeepRadio UI state: %s", text or "")

    def _make_caption(self, text, theme, align_end=False):
        cap = _CaptionLabel()
        cap.set_use_markup(True)
        cap.set_markup(
            "<span foreground='{}'>{}</span>".format(
                theme["caption"], escape_pango(text)))
        cap.set_halign(Gtk.Align.END if align_end else Gtk.Align.START)
        return cap

    def _append(self, who, text):
        """追加一条聊天气泡: 用户靠右蓝底, DeepRadio 靠左白底。"""
        is_user = who in _USER_ROLES
        kind = "user" if is_user else (
            "meta" if who not in _USER_ROLES and who != "DeepRadio" else "agent")
        theme = _THEME[kind]
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        wrap.set_hexpand(True)
        wrap.set_halign(Gtk.Align.FILL)
        wrap.set_margin_start(6)
        wrap.set_margin_end(6)
        wrap.pack_start(self._make_caption(who, theme, is_user), False, False, 0)

        bubble = _ChatBubble(theme["fill"], theme["border"])

        label = _FlowLabel()
        label.set_margin_top(10)
        label.set_margin_bottom(10)
        label.set_margin_start(12)
        label.set_margin_end(12)
        body = text or ""
        if is_user:
            inner = escape_pango(body)
        else:
            inner = markdown_to_pango(body) or escape_pango(body)
        label.set_use_markup(True)
        try:
            label.set_markup(
                "<span foreground='{}'>{}</span>".format(
                    theme["text"], inner))
        except Exception:  # noqa: BLE001
            label.set_use_markup(False)
            label.set_text(body)
        if self._chat_width >= 80:
            label.set_size_request(self._body_wrap_width(), -1)
        bubble.pack_start(label, False, False, 0)
        wrap.pack_start(bubble, False, False, 0)
        wrap.show_all()
        self._log_box.pack_start(wrap, False, False, 0)
        self._scroll_to_bottom()

    def _append_image(self, title, path):
        """把一张产物图缩放后内联展示(靠左,与 DeepRadio 气泡对齐)。"""
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        wrap.set_halign(Gtk.Align.FILL)
        wrap.set_hexpand(True)
        wrap.set_margin_start(6)
        wrap.set_margin_end(6)
        wrap.pack_start(
            self._make_caption(title, _THEME["agent"]), False, False, 0)
        img_w = 280
        if self._chat_width >= 80:
            img_w = min(280, max(80, self._body_wrap_width()))
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, img_w, 210, True)
        except Exception as e:  # noqa: BLE001
            log.warning("加载产物图失败 %s: %s", path, e)
            self._append(title, "(Failed to load image: {})".format(path))
            return
        bubble = _ChatBubble(_THEME["agent"]["fill"], _THEME["agent"]["border"])
        img = Gtk.Image.new_from_pixbuf(pixbuf)
        img.set_margin_top(8)
        img.set_margin_bottom(8)
        img.set_margin_start(8)
        img.set_margin_end(8)
        img.set_halign(Gtk.Align.START)
        bubble.pack_start(img, False, False, 0)
        wrap.pack_start(bubble, False, False, 0)
        wrap.show_all()
        self._log_box.pack_start(wrap, False, False, 0)
        self._scroll_to_bottom()

    def _new_conversation_card(self, title, fill, border):
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        wrap.set_hexpand(True)
        wrap.set_halign(Gtk.Align.FILL)
        wrap.set_margin_start(6)
        wrap.set_margin_end(6)
        wrap.pack_start(
            self._make_caption(title, _THEME["agent"]), False, False, 0
        )
        bubble = _ChatBubble(fill, border, radius=12)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_top(10)
        body.set_margin_bottom(10)
        body.set_margin_start(10)
        body.set_margin_end(10)
        bubble.pack_start(body, False, False, 0)
        wrap.pack_start(bubble, False, False, 0)
        return wrap, body

    def _replace_interaction_cards(self, spec, workflow, claims, pending):
        for widget in list(self._live_cards):
            parent = widget.get_parent()
            if parent is not None:
                parent.remove(widget)
        self._live_cards = []
        view = present(spec=spec, workflow=workflow, claims=claims)
        specification = dict(view.get("specification") or {})
        meaningful = [
            row for row in specification.get("rows") or []
            if row.get("key") != "summary"
        ]
        if meaningful:
            self._append_specification_card(
                specification, spec, workflow, pending
            )
        diagnosis = dict(view.get("diagnosis") or {})
        if diagnosis.get("visible"):
            self._append_diagnosis_card(diagnosis)
        for widget in self._live_cards:
            widget.show_all()
        if self._chat_width >= 80:
            self._constrain_chat_bodies(
                self._log_box, self._body_wrap_width()
            )
            self._log_box.queue_resize()
        self._scroll_to_bottom()

    def _append_specification_card(self, specification, spec, workflow, pending):
        status = str(specification.get("status") or "draft")
        title = "Radio Specification"
        wrap, body = self._new_conversation_card(title, "#F5F2FF", "#8A6FD1")
        expander = Gtk.Expander(label="View Current Specification")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        rows = sorted(
            specification.get("rows") or [],
            key=lambda item: (
                0 if item.get("requirement") == "required" else 1,
                str(item.get("label") or ""),
            ),
        )
        source_colors = {
            "User": "#2F6FED",
            "Protocol Default": "#6B4EFF",
            "Safety Default": "#C45B08",
            "Derived": "#6B4EFF",
            "Canvas": "#2F6FED",
            "Unresolved": "#7A7A7A",
        }
        current_group = ""
        for row in rows:
            group = str(row.get("group") or "Added") + ":"
            if group != current_group:
                group_label = _FlowLabel(label=group)
                group_label.set_markup("<b>{}</b>".format(escape_pango(group)))
                content.pack_start(group_label, False, False, 0)
                current_group = group
            item = Gtk.EventBox()
            row_color = Gdk.RGBA()
            row_color.parse(
                "#FFF4E5" if row.get("needs_confirmation")
                else "#EDF4FF" if row.get("requirement") == "required"
                else "#F3F0FA"
            )
            item.override_background_color(Gtk.StateFlags.NORMAL, row_color)
            row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row_box.set_margin_top(5)
            row_box.set_margin_bottom(5)
            row_box.set_margin_start(4)
            row_box.set_margin_end(4)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            name = _FlowLabel(label=str(row.get("label") or row.get("key") or ""))
            name.set_markup("<b>{}</b>".format(escape_pango(name.get_text())))
            header.pack_start(name, True, True, 0)
            source = str(row.get("source") or "System")
            badge = Gtk.Label()
            badge.set_halign(Gtk.Align.END)
            badge.set_markup(
                "<span foreground='{}'>[{}]</span>".format(
                    source_colors.get(source, "#52606D"),
                    escape_pango(source),
                )
            )
            header.pack_end(badge, False, False, 0)
            row_box.pack_start(header, False, False, 0)
            value = _FlowLabel(label=str(row.get("value") or "?"))
            row_box.pack_start(value, False, False, 0)
            item.add(row_box)
            content.pack_start(item, False, False, 0)
        expander.add(content)
        expander.set_expanded(status != "confirmed")
        body.pack_start(expander, False, False, 0)
        self._log_box.pack_start(wrap, False, False, 0)
        self._live_cards.append(wrap)

    def _append_diagnosis_card(self, diagnosis):
        wrap, body = self._new_conversation_card(
            "Diagnosis", "#F3F8FF", "#4C78C2"
        )
        for item in diagnosis.get("findings") or []:
            status = str(item.get("status_id") or "unknown")
            marker = "✓" if status == "pass" else "✕" if status == "fail" else "?"
            text = "{} {} — {}".format(
                marker, item.get("label") or "Check", item.get("status") or "Unknown"
            )
            if item.get("remediation") and status != "pass":
                text += "\nRecommendation: " + str(item.get("remediation"))
            label = _FlowLabel(label=text)
            body.pack_start(label, False, False, 0)
        self._log_box.pack_start(wrap, False, False, 0)
        self._live_cards.append(wrap)

    def _scroll_to_bottom(self):
        adj = self._scroll.get_vadjustment()
        if adj is not None:
            GLib.idle_add(lambda: adj.set_value(
                adj.get_upper() - adj.get_page_size()) or False)
