"""
Agent 右侧对话框面板 (旧版 GTK 界面)。

在 MainWindow 右侧提供一个人机对话面板: 用户输入自然语言需求
(如 "生成一个调制方式为 QPSK 的 BLE 波形, 包含信息 xxx"), 面板调用
后端 ``grc.agent.build_flow_graph_from_text`` 生成 .grc 文件, 再通过
``open_flow_graph`` 信号通知 MainWindow 载入画布, 用户可继续在 UI 交互。

SPDX-License-Identifier: GPL-2.0-or-later
"""

import logging
import os
import threading

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib, GObject

log = logging.getLogger(__name__)


class AgentPanel(Gtk.VBox):
    """右侧 Agent 对话面板。

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
        # 多轮对话历史: [(role, content), ...], role 为 'user' / 'assistant'。
        self._history = []

        # ---- 聊天历史显示区 ----
        self.history = Gtk.TextView()
        self.history.set_editable(False)
        self.history.set_cursor_visible(False)
        self.history.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._buffer = self.history.get_buffer()

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.history)
        self.pack_start(scroll, expand=True, fill=True, padding=0)

        # ---- 输入区: 文本框 + 发送按钮 ----
        input_box = Gtk.HBox()
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(
            "描述你要的波形, 例如: 生成 QPSK 的 BLE 波形, 包含信息 xxx")
        self.entry.connect('activate', self._on_send)
        input_box.pack_start(self.entry, expand=True, fill=True, padding=0)

        self.send_button = Gtk.Button(label="发送")
        self.send_button.connect('clicked', self._on_send)
        input_box.pack_start(self.send_button, expand=False, fill=False,
                             padding=0)

        self.pack_start(input_box, expand=False, fill=False, padding=2)

        self.set_size_request(320, -1)

        self._append("Agent", "你好! 请描述你需要的波形, 我会尝试生成流图并载入画布。")

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_send(self, _widget):
        if self._busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self._append("我", text)
        self.entry.set_text('')
        self._history.append(("user", text))
        self._set_busy(True)
        # 耗时的 LLM/建图放到子线程, 避免卡死 GTK 主循环。
        history = list(self._history)
        threading.Thread(target=self._handle, args=(text, history),
                         daemon=True).start()

    def _handle(self, text, history):
        """子线程: 调后端建图并存 .grc。UI 更新一律回主线程。"""
        try:
            from grc.agent import build_flow_graph_from_text
            grc_path = build_flow_graph_from_text(
                text, self.platform, history=history)
            GLib.idle_add(self._on_done, grc_path)
        except Exception as e:  # noqa: BLE001
            log.exception("Agent 生成流图失败")
            GLib.idle_add(self._on_error, str(e))

    def _on_done(self, grc_path):
        """主线程: 生成成功, 回显并请求打开。"""
        msg = "已生成 {}, 正在载入画布…".format(os.path.basename(grc_path))
        self._append("Agent", msg)
        self._history.append(("assistant", msg))
        self._set_busy(False)
        self.emit('open_flow_graph', grc_path)
        return False  # idle_add 只跑一次

    def _on_error(self, message):
        """主线程: 生成失败, 回显错误。"""
        self._append("Agent", "出错了: {}".format(message))
        self._history.append(("assistant", "出错了: {}".format(message)))
        self._set_busy(False)
        return False

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _set_busy(self, busy):
        self._busy = busy
        self.entry.set_sensitive(not busy)
        self.send_button.set_sensitive(not busy)
        self.send_button.set_label("生成中…" if busy else "发送")

    def _append(self, who, text):
        """把一行对话追加到历史区并滚动到底部。"""
        end = self._buffer.get_end_iter()
        self._buffer.insert(end, "{}: {}\n".format(who, text))
        # 滚动到底部
        mark = self._buffer.create_mark(None, self._buffer.get_end_iter(),
                                        False)
        self.history.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        return False
