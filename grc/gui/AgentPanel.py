"""
Agent 右侧对话面板 (多轮协商版 GTK 界面)。

在 MainWindow 右侧提供一个人机对话面板:

* **多轮协商 Agent**(默认): 持有 ``ServiceAgent``，按闭环模式委派六个领域
  Subagent，并通过 SharedState 展示可追溯 Spec 与 Claim/Evidence；
  产出 .grc 时 emit ``open_flow_graph`` 让 MainWindow 载入画布。
* **一句话直出 (baseline)**: 勾选开关后走 ``build_flow_graph_from_text``,
  LLM 直接产 .grc, 作为论文对照组。
* **专业度档位**(创新 B): 下拉可选 自适应 / 小白 / 学生 / 专家; 选具体档位则
  钉档 (pin), 选"自适应"则放开 (unpin) 让画像随对话自适应。

所有产物统一输出到工程根目录下的 ``local/output/``, 便于查找与管理。

SPDX-License-Identifier: GPL-2.0-or-later
"""

import logging
import os
import threading

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, GObject, GdkPixbuf

from .ClaimsPanel import ClaimsPanel

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
    ("自适应", (True, None)),
    ("小白", (False, "novice")),
    ("学生", (False, "student")),
    ("专家", (False, "expert")),
]

#: 产物图字段 -> 中文标题, 按此顺序内联展示。
_ARTIFACT_IMAGES = [
    ("constellation_png", "星座图"),
    ("spectrum_png", "频谱图"),
    ("eye_png", "眼图"),
]


class AgentPanel(Gtk.VBox):
    """右侧 Agent 对话面板 (多轮协商 + 档位 + 内联产物图)。

    对外发出一个信号:
        open_flow_graph(str): 请求 MainWindow 打开给定路径的 .grc 文件。
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

        # 多轮协商 Agent 实例 (惰性创建, 避免无 gnuradio 时导入报错)。
        self._agent = None
        # baseline 一句话直出的历史 [(role, content), ...]。
        self._baseline_history = []

        # ---- 顶部控制条: 档位下拉 + baseline 开关 + 重置 ----
        ctrl = Gtk.HBox()
        ctrl.pack_start(Gtk.Label(label="专业度:"), False, False, 2)

        self.level_combo = Gtk.ComboBoxText()
        for label, _ in _LEVEL_CHOICES:
            self.level_combo.append_text(label)
        self.level_combo.set_active(0)  # 默认"自适应"
        self.level_combo.connect('changed', self._on_level_changed)
        ctrl.pack_start(self.level_combo, False, False, 2)

        self.baseline_check = Gtk.CheckButton(label="一句话直出(baseline)")
        ctrl.pack_start(self.baseline_check, False, False, 2)

        self.reset_button = Gtk.Button(label="重置")
        self.reset_button.connect('clicked', self._on_reset)
        ctrl.pack_end(self.reset_button, False, False, 2)

        self.pack_start(ctrl, expand=False, fill=False, padding=2)

        # ---- 聊天历史显示区: 用 VBox 容纳文本气泡 + 内联图片 ----
        self._log_box = Gtk.VBox()
        self._log_box.set_spacing(4)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add_with_viewport(self._log_box)
        self._scroll = scroll
        self.pack_start(scroll, expand=True, fill=True, padding=0)

        self.claims_panel = ClaimsPanel()
        self.pack_start(self.claims_panel, expand=False, fill=True, padding=2)

        # ---- 状态栏: 显示当前阶段 / 档位 ----
        self.status = Gtk.Label(label="就绪")
        self.status.set_halign(Gtk.Align.START)
        self.pack_start(self.status, expand=False, fill=False, padding=2)

        # ---- 输入区: 文本框 + 发送按钮 ----
        input_box = Gtk.HBox()
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(
            "描述你要的链路, 例如: 用 BPSK 过 AWGN 看星座图")
        self.entry.connect('activate', self._on_send)
        input_box.pack_start(self.entry, expand=True, fill=True, padding=0)

        self.send_button = Gtk.Button(label="发送")
        self.send_button.connect('clicked', self._on_send)
        input_box.pack_start(self.send_button, expand=False, fill=False,
                             padding=0)

        self.pack_start(input_box, expand=False, fill=False, padding=2)

        self.set_size_request(360, -1)

        self._append("Agent",
                     "你好! 我会通过多轮协商帮你设计通信链路(建图→仿真→调参)。\n"
                     "产物统一保存在 local/output/。请描述你的需求。")

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
            self._agent._platform = self.platform
            # 产物统一落到 local/output/ (通过 ctx 兼容层)。
            self._agent.ctx.tool_ctx.out_dir = self._out_dir
            # 把当前下拉档位应用到画像。
            self._apply_level_to_agent()
        return self._agent

    def _apply_level_to_agent(self):
        """把档位下拉的选择应用到 Agent 的 profile / adaptive 开关。"""
        if self._agent is None:
            return
        idx = self.level_combo.get_active()
        if idx < 0:
            idx = 0
        adaptive, pinned = _LEVEL_CHOICES[idx][1]
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
        self._set_status("专业度档位: {}".format(label))

    def _on_reset(self, _widget):
        if self._busy:
            return
        self._agent = None
        self._baseline_history = []
        for child in self._log_box.get_children():
            self._log_box.remove(child)
        self.claims_panel.clear()
        self._append("Agent", "已重置会话。请描述新的需求。")
        self._set_status("就绪")

    def _on_send(self, _widget):
        if self._busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self._append("我", text)
        self.entry.set_text('')
        self._set_busy(True)

        baseline = self.baseline_check.get_active()
        if baseline:
            self._baseline_history.append(("user", text))
            history = list(self._baseline_history)
            threading.Thread(target=self._handle_baseline,
                             args=(text, history), daemon=True).start()
        else:
            threading.Thread(target=self._handle_agent,
                             args=(text,), daemon=True).start()

    # -- 多轮协商 Agent 路径 --------------------------------------------- #
    def _handle_agent(self, text):
        """子线程: 走 agent.step 多轮协商。UI 更新回主线程。"""
        try:
            agent = self._ensure_agent()
            reply = agent.step(text)
            GLib.idle_add(self._on_agent_reply, reply)
        except Exception as e:  # noqa: BLE001
            log.exception("Agent step 失败")
            GLib.idle_add(self._on_error, str(e))

    def _on_agent_reply(self, reply):
        """主线程: 回显 agent 叙述 + 内联产物图 + 载入 .grc。"""
        self._append("Agent", reply.text or "(无输出)")

        artifacts = reply.artifacts or {}
        # 内联展示产物图。
        for key, title in _ARTIFACT_IMAGES:
            path = artifacts.get(key)
            if path and os.path.exists(path):
                self._append_image(title, path)
        # 指标摘要。
        metrics = artifacts.get("metrics")
        if isinstance(metrics, dict) and metrics:
            summary = ", ".join(
                "{}={}".format(k, self._fmt(v)) for k, v in metrics.items())
            self._append("指标", summary)
        self.claims_panel.update_data(
            getattr(reply, "claims", []),
            getattr(reply, "spec_digest", {}),
        )
        # 产出 .grc -> 载入画布。
        grc_path = artifacts.get("grc_path") or artifacts.get("path")
        if grc_path and str(grc_path).endswith(".grc") \
                and os.path.exists(grc_path):
            self._append("Agent",
                         "已生成 {}, 正在载入画布…".format(
                             os.path.basename(grc_path)))
            self.emit('open_flow_graph', grc_path)

        # 状态栏: 阶段 + 是否需确认。
        stage = getattr(reply, "stage", "") or ""
        tip = " (等待你确认/回复)" if getattr(reply, "needs_confirmation",
                                            False) else ""
        level = self._agent.ctx.profile.level if self._agent else "?"
        self._set_status("阶段: {} | 档位: {}{}".format(stage, level, tip))
        self._set_busy(False)
        return False

    # -- baseline 一句话直出路径 ----------------------------------------- #
    def _handle_baseline(self, text, history):
        """子线程: 走 build_flow_graph_from_text 直出 .grc。"""
        try:
            from grc.agent import build_flow_graph_from_text
            grc_path = build_flow_graph_from_text(
                text, self.platform, out_dir=self._out_dir, history=history)
            GLib.idle_add(self._on_baseline_done, grc_path)
        except Exception as e:  # noqa: BLE001
            log.exception("baseline 生成流图失败")
            GLib.idle_add(self._on_error, str(e))

    def _on_baseline_done(self, grc_path):
        msg = "已生成 {}, 正在载入画布…".format(os.path.basename(grc_path))
        self._append("Agent", msg)
        self.claims_panel.clear()
        self._baseline_history.append(("assistant", msg))
        self._set_busy(False)
        self.emit('open_flow_graph', grc_path)
        return False

    def _on_error(self, message):
        self._append("Agent", "出错了: {}".format(message))
        self._set_status("出错")
        self._set_busy(False)
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
        self.baseline_check.set_sensitive(not busy)
        self.reset_button.set_sensitive(not busy)
        self.send_button.set_label("处理中…" if busy else "发送")

    def _set_status(self, text):
        self.status.set_text(text)

    def _append(self, who, text):
        """把一行对话追加为一个文本标签。"""
        label = Gtk.Label(label="{}: {}".format(who, text or ""))
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        label.set_halign(Gtk.Align.START)
        label.set_xalign(0.0)
        label.set_selectable(True)
        self._log_box.pack_start(label, False, False, 0)
        label.show()
        self._scroll_to_bottom()

    def _append_image(self, title, path):
        """把一张产物图缩放后内联展示。"""
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, 320, 240, True)
        except Exception as e:  # noqa: BLE001
            log.warning("加载产物图失败 %s: %s", path, e)
            self._append(title, "(图片加载失败: {})".format(path))
            return
        cap = Gtk.Label(label=title)
        cap.set_halign(Gtk.Align.START)
        self._log_box.pack_start(cap, False, False, 0)
        img = Gtk.Image.new_from_pixbuf(pixbuf)
        img.set_halign(Gtk.Align.START)
        self._log_box.pack_start(img, False, False, 0)
        cap.show()
        img.show()
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        adj = self._scroll.get_vadjustment()
        if adj is not None:
            GLib.idle_add(lambda: adj.set_value(
                adj.get_upper() - adj.get_page_size()) or False)
